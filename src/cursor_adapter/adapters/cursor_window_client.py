from __future__ import annotations

"""
Windows Cursor UI automation + Ollama HTTP client for OpenClaw-style agents.

- ``CursorWindowClient``: drives the Cursor desktop chat window (UIA).
- ``OllamaLLMClient``: calls local/remote Ollama (native ``/api/chat`` or OpenAI ``/v1``).
- ``OpenClawLLMClient`` / ``create_openclaw_llm_client()``: pick backend via env
  ``OPENCLAW_LLM_BACKEND=ollama|cursor`` (default ``ollama``).
"""

import difflib
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import os

import pyperclip
import win32api
import win32gui
import win32con
from PIL import ImageGrab
from pywinauto import Application
from pywinauto.keyboard import send_keys

THREE_DOTS_CLASS_HINT = "composer-unified-dropdown !border-none !rounded-full"
MESSAGE_CONTAINER_CLASS = "composer-messages-container"
ICON_BUTTON_CLASS = (
    "anysphere-icon-button bg-[transparent] border-none text-[var(--cursor-text-primary)] "
    "flex w-4 items-center justify-center"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CursorWindowSettings:
    title_regex: str
    wait_seconds: int
    min_response_chars: int = 80
    max_retries: int = 2
    enable_clipboard_fallback: bool = False
    require_three_dots_completion: bool = True
    # When the latest assistant message is below the fold, wheel-scroll this many times
    # (batches × ticks) inside composer-messages-container before giving up on three-dots.
    three_dots_scroll_batches: int = 18
    three_dots_scroll_wheel_ticks_per_batch: int = 4
    # Max wheel batches while chasing "scroll reached bottom" (image stable).
    three_dots_scroll_to_bottom_max_batches: int = 48
    # When enabled, Cursor must write the final JSON result to disk at a provided path.
    # The generator will read/parse that file instead of scraping chat text.
    use_disk_json_response: bool = True
    # Directory for response JSON files (under project_root). If None, defaults to
    # <project_root>/.cursor_llm/window_responses
    disk_json_response_dir: Optional[str] = None


def _use_disk_json_response_from_env(default: bool) -> bool:
    raw = os.getenv("GEN_CURSOR_WINDOW_USE_DISK_JSON", "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y"}


def _disk_json_response_dir_from_env(default: Optional[str]) -> Optional[str]:
    raw = os.getenv("GEN_CURSOR_WINDOW_DISK_JSON_DIR", "").strip()
    return raw or default


def _cursor_disk_json_footer(response_path: Path) -> str:
    """
    Force a file-based JSON response so the generator can deterministically read it.
    """
    return (
        "\n\n---\n\n"
        "Output requirement (CRITICAL):\n"
        f"1) Write the final result as a single JSON object to this exact absolute path:\n"
        f"{response_path.resolve()}\n"
        "2) The JSON must be UTF-8 and valid JSON (no trailing commas).\n"
        "3) Overwrite the file if it already exists.\n"
        "4) Do NOT paste the JSON into chat. After writing the file, reply only: OK\n"
    )


def _cursor_disk_json_ack_prompt(response_path: Path) -> str:
    """
    Second queued instruction: write only the JSON receipt file.
    This reduces \"forget to write\" failures by making the receipt a dedicated action.
    """
    return (
        "ACK / RECEIPT TASK (do this now):\n"
        "Write ONLY the final JSON result object to disk (no other file modifications).\n"
        + _cursor_disk_json_footer(response_path)
    )


def _wait_for_json_response_file(response_path: Path, *, timeout_s: int) -> str:
    """
    Wait until the response JSON file exists and contains a parseable JSON object.
    Returns the raw JSON text.
    """
    deadline = time.time() + max(1, int(timeout_s))
    last_err: str | None = None
    logger.info("[cursor-window] waiting for JSON response file: %s (timeout=%ss)", response_path, timeout_s)
    while time.time() < deadline:
        time.sleep(0.5)
        if not response_path.exists():
            continue
        try:
            # Accept UTF-8 files with BOM emitted by some editors/agents.
            raw = response_path.read_text(encoding="utf-8-sig").strip()
        except OSError as e:
            last_err = f"read error: {e}"
            logger.warning("[cursor-window] response file read error: %s", e)
            continue
        if not raw:
            last_err = "empty file"
            logger.warning("[cursor-window] response file exists but is empty: %s", response_path)
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            last_err = f"json decode: {e}"
            logger.warning("[cursor-window] response file json decode failed: %s", e)
            continue
        if not isinstance(obj, dict):
            last_err = f"json is not an object: {type(obj).__name__}"
            logger.warning("[cursor-window] response JSON is not an object: %s", type(obj).__name__)
            continue
        # Minimal sanity: many generator nodes expect status/files/message.
        if "status" not in obj:
            last_err = "missing 'status' field"
            logger.warning("[cursor-window] response JSON missing status field: %s", response_path)
            continue
        logger.info("[cursor-window] response file ready: %s", response_path)
        return raw
    logger.error(
        "[cursor-window] timed out waiting for JSON response file: %s (last_err=%s)",
        response_path,
        last_err or "unknown",
    )
    raise TimeoutError(
        "Timed out waiting for Cursor disk JSON response file. "
        f"Expected: {response_path}. Last error: {last_err or 'unknown'}"
    )


def _three_dots_scroll_batches_from_env(default: int) -> int:
    raw = os.getenv("GEN_CURSOR_THREE_DOTS_SCROLL_BATCHES", "").strip()
    if not raw:
        return max(0, default)
    try:
        return max(0, int(raw))
    except ValueError:
        return max(0, default)


def _three_dots_scroll_to_bottom_max_batches_from_env(default: int) -> int:
    raw = os.getenv("GEN_CURSOR_SCROLL_TO_BOTTOM_MAX_BATCHES", "").strip()
    if not raw:
        return max(1, default)
    try:
        return max(1, int(raw))
    except ValueError:
        return max(1, default)


def _three_dots_wheel_ticks_from_env(default: int) -> int:
    raw = os.getenv("GEN_CURSOR_THREE_DOTS_SCROLL_TICKS", "").strip()
    if not raw:
        return max(1, default)
    try:
        return max(1, int(raw))
    except ValueError:
        return max(1, default)


def _enumerate_window_handles() -> List[int]:
    hwnds: List[int] = []

    def callback(hwnd, _extra):
        if win32gui.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return hwnds


def _traverse_handles_via_findwindow() -> List[int]:
    """
    Traverse top-level window handles using Win32 handle chain APIs:
    GetTopWindow + GetWindow(GW_HWNDNEXT).
    """
    hwnds: List[int] = []
    hwnd = win32gui.GetTopWindow(None)
    guard = 0
    while hwnd and guard < 10000:
        guard += 1
        try:
            if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
                hwnds.append(hwnd)
        except Exception:
            pass
        hwnd = win32gui.GetWindow(hwnd, win32con.GW_HWNDNEXT)
    return hwnds


def _find_cursor_hwnd(title_regex: str) -> int:
    pattern = re.compile(title_regex)
    # Primary strategy: traverse handle chain (FindWindow-style).
    for hwnd in _traverse_handles_via_findwindow():
        title = win32gui.GetWindowText(hwnd)
        if title and pattern.search(title):
            return hwnd
    # Fallback strategy: EnumWindows.
    for hwnd in _enumerate_window_handles():
        title = win32gui.GetWindowText(hwnd)
        if title and pattern.search(title):
            return hwnd
    raise RuntimeError(f"Cursor window not found by regex: {title_regex}")


def _find_cursor_hwnd_with_retry(title_regex: str, *, timeout_s: float = 8.0) -> int:
    """
    First run often races Cursor startup: window handle isn't visible yet.
    Retry for a short duration before failing.
    """
    deadline = time.time() + max(0.5, float(timeout_s))
    last_err: str | None = None
    while time.time() < deadline:
        try:
            return _find_cursor_hwnd(title_regex)
        except RuntimeError as e:
            last_err = str(e)
        time.sleep(0.5)
    raise RuntimeError(last_err or f"Cursor window not found by regex: {title_regex}")


def _activate_window(hwnd: int) -> None:
    # Do not change window size/state, only bring it to foreground.
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.25)


def _safe_descendants(app_window):
    """
    pywinauto may crash while wrapping UIA descendants when an element has
    element_info.control_type == None (observed as KeyError in UIAWrapper).
    Enumerate defensively and skip invalid nodes.
    """
    try:
        for ctrl in app_window.descendants():
            yield ctrl
        return
    except Exception:
        pass

    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
    except Exception:
        return

    try:
        infos = app_window.element_info.descendants()
    except Exception:
        return

    for info in infos:
        try:
            if getattr(info, "control_type", None) is None:
                continue
            yield UIAWrapper(info)
        except Exception:
            continue


def _collect_text_snapshot(app_window) -> List[str]:
    texts: List[str] = []
    for ctrl in _safe_descendants(app_window):
        try:
            txt = (ctrl.window_text() or "").strip()
            if txt and len(txt) >= 20:
                texts.append(txt)
        except Exception:
            continue
    return sorted(set(texts))


def _closest_text_scope(input_ctrl, fallback_window):
    """
    Try to scope text extraction around chat panel to avoid terminal/status noise.
    """
    current = input_ctrl
    for _ in range(4):
        try:
            parent = current.parent()
        except Exception:
            break
        if parent is None:
            break
        current = parent
    return current if current is not None else fallback_window


def _extract_delta_text(before: List[str], after: List[str]) -> str:
    before_set = set(before)
    candidates = [t for t in after if t not in before_set]
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    # Fallback: choose longest changed block.
    before_blob = "\n".join(before)
    after_blob = "\n".join(after)
    diff = list(difflib.unified_diff(before_blob.splitlines(), after_blob.splitlines()))
    if diff:
        return "\n".join(diff[-120:])
    return ""


def _safe_rect(ctrl) -> Optional[Tuple[int, int, int, int]]:
    try:
        r = ctrl.rectangle()
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def _find_chat_input_control(app_window):
    """
    Prefer targeting concrete input controls over global key sends.
    """
    # Hard priority: Cursor chat input control class.
    for ctrl in _safe_descendants(app_window):
        try:
            info = getattr(ctrl, "element_info", None)
            class_name = (getattr(info, "class_name", "") or "").strip().lower()
            control_type = (getattr(info, "control_type", "") or "").strip().lower()
            if (
                class_name == "aislash-editor-input"
                and control_type in {"edit", "document"}
                and ctrl.is_visible()
                and ctrl.is_enabled()
            ):
                return ctrl
        except Exception:
            continue

    win_rect = _safe_rect(app_window)
    candidates = []
    for ctrl in _safe_descendants(app_window):
        try:
            control_type = (ctrl.element_info.control_type or "").lower()
            name = (ctrl.element_info.name or "").lower()
            auto_id = (ctrl.element_info.automation_id or "").lower()
            if control_type in {"edit", "document"}:
                score = 0
                if "chat" in name or "message" in name or "ask" in name:
                    score += 3
                if "input" in name or "editor" in name or "textbox" in name:
                    score += 2
                if "chat" in auto_id or "input" in auto_id or "editor" in auto_id:
                    score += 2
                if ctrl.is_visible() and ctrl.is_enabled():
                    score += 1
                # Prefer controls located at the lower-right area.
                rect = _safe_rect(ctrl)
                if rect and win_rect:
                    wl, wt, wr, wb = win_rect
                    cl, ct, cr, cb = rect
                    if cb >= wb - int((wb - wt) * 0.45):  # bottom area
                        score += 2
                    if cr >= wr - int((wr - wl) * 0.45):  # right area
                        score += 2
                candidates.append((score, ctrl))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _find_aislash_input_control(app_window):
    for ctrl in _safe_descendants(app_window):
        try:
            info = getattr(ctrl, "element_info", None)
            class_name = (getattr(info, "class_name", "") or "").strip().lower()
            control_type = (getattr(info, "control_type", "") or "").strip().lower()
            if (
                class_name == "aislash-editor-input"
                and control_type in {"edit", "document"}
                and ctrl.is_visible()
                and ctrl.is_enabled()
            ):
                return ctrl
        except Exception:
            continue
    return None


def _collect_buttons_near_chat_anchor(app_window) -> List[Dict[str, object]]:
    anchor = _find_aislash_input_control(app_window)
    if anchor is None:
        return _collect_buttons_chat_band_fallback(app_window)
    anchor_rect = _safe_rect(anchor)
    if not anchor_rect:
        return _collect_buttons_chat_band_fallback(app_window)
    al, at, ar, ab = anchor_rect
    aw = max(1, ar - al)
    ah = max(1, ab - at)

    # Expand to nearby chat action area (send/stop usually at right side of input).
    scope_left = al - int(aw * 0.25)
    scope_top = at - int(ah * 2.0)
    scope_right = ar + int(aw * 0.35)
    scope_bottom = ab + int(ah * 2.0)

    candidates: List[Dict[str, object]] = []
    for ctrl in _safe_descendants(app_window):
        try:
            info = getattr(ctrl, "element_info", None)
            control_type = (getattr(info, "control_type", "") or "").strip().lower()
            if control_type not in {"button", "image", "hyperlink"}:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            if hasattr(ctrl, "is_enabled") and not ctrl.is_enabled():
                continue
            rect = _safe_rect(ctrl)
            if not rect:
                continue
            cl, ct, cr, cb = rect
            cx = (cl + cr) / 2.0
            cy = (ct + cb) / 2.0
            if not (scope_left <= cx <= scope_right and scope_top <= cy <= scope_bottom):
                continue
            row = _element_descriptor(ctrl)
            row["path"] = None
            row["anchor_rect"] = [al, at, ar, ab]
            row["scope_rect"] = [scope_left, scope_top, scope_right, scope_bottom]
            row["center"] = {"x": round(cx, 1), "y": round(cy, 1)}
            candidates.append(row)
        except Exception:
            continue
    return candidates


def _collect_buttons_chat_band_fallback(app_window) -> List[Dict[str, object]]:
    """
    Fallback when chat input anchor not found:
    search a right-bottom band above status bar, and filter known status labels.
    """
    win_rect = _safe_rect(app_window)
    if not win_rect:
        return []
    wl, wt, wr, wb = win_rect
    ww = max(1, wr - wl)
    wh = max(1, wb - wt)

    # Chat panel area is typically above status bar, still at right-bottom.
    y_min = wt + int(wh * 0.72)
    y_max = wt + int(wh * 0.94)
    x_min = wl + int(ww * 0.68)

    skip_names = {"notifications", "python", "utf-8", "lf"}
    candidates: List[Dict[str, object]] = []
    for ctrl in _safe_descendants(app_window):
        try:
            info = getattr(ctrl, "element_info", None)
            control_type = (getattr(info, "control_type", "") or "").strip().lower()
            if control_type not in {"button", "image", "hyperlink"}:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            rect = _safe_rect(ctrl)
            if not rect:
                continue
            cl, ct, cr, cb = rect
            cx = (cl + cr) / 2.0
            cy = (ct + cb) / 2.0
            if not (x_min <= cx <= wr and y_min <= cy <= y_max):
                continue
            name = (getattr(info, "name", "") or "").strip()
            if name.lower() in skip_names:
                continue
            row = _element_descriptor(ctrl)
            row["path"] = None
            row["scope_rect"] = [x_min, y_min, wr, y_max]
            row["center"] = {"x": round(cx, 1), "y": round(cy, 1)}
            candidates.append(row)
        except Exception:
            continue
    return candidates


def _get_chat_action_image_candidate(app_window) -> Optional[Dict[str, object]]:
    """
    Return the likely chat action icon (send/stop) near chat input anchor.
    """
    candidates = _collect_buttons_near_chat_anchor(app_window)
    if not candidates:
        return None
    image_like = [
        c
        for c in candidates
        if str(c.get("control_type", "")).lower() in {"image", "button", "hyperlink"}
    ]
    if not image_like:
        return None
    image_like.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
    return image_like[0]


def _image_signature_from_rect(rect: List[int]) -> Optional[str]:
    """
    Capture image region and hash it for motion/state detection.
    """
    if not rect or len(rect) != 4:
        return None
    try:
        l, t, r, b = rect
        if r - l <= 1 or b - t <= 1:
            return None
        img = ImageGrab.grab(bbox=(l, t, r, b))
        raw = img.tobytes()
        return hashlib.md5(raw).hexdigest()
    except Exception:
        return None


def _is_chat_action_image_busy(app_window, prev_sig: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Infer generating/busy state from chat action image dynamics.
    Returns (is_busy, current_signature).
    """
    icon = _get_chat_action_image_candidate(app_window)
    if not icon:
        # No icon detected means we cannot claim busy.
        return False, prev_sig
    rect = icon.get("rect")
    cur_sig = _image_signature_from_rect(rect) if rect else None
    if cur_sig is None:
        return False, prev_sig
    if prev_sig is None:
        return True, cur_sig
    # If icon image keeps changing, likely still generating.
    if cur_sig != prev_sig:
        return True, cur_sig
    return False, cur_sig


def _find_composer_messages_container(app_window):
    """First visible composer-messages-container under app_window."""
    for ctrl in _safe_descendants(app_window):
        try:
            info = getattr(ctrl, "element_info", None)
            class_name = (getattr(info, "class_name", "") or "").strip()
            if class_name != MESSAGE_CONTAINER_CLASS:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            return ctrl
        except Exception:
            continue
    return None


def _scroll_messages_container_to_bottom(
    container,
    *,
    max_batches: int,
    ticks_per_batch: int,
) -> None:
    """
    Focus transcript, click just inside the left edge of the messages area, then
    wheel down until viewport capture stops changing (treated as scrollbar at bottom).
    Ctrl+End is not used; Electron/WebView often ignores it for this pane.
    """
    rect_t = _safe_rect(container)
    if not rect_t:
        return
    l, t, r, b = rect_t
    if r - l <= 4 or b - t <= 4:
        return
    # Slightly right of the left border so the click hits the scrollable strip, not chrome.
    inset = max(4, min(14, (r - l) // 24))
    cx, cy = l + inset, (t + b) // 2
    try:
        container.set_focus()
    except Exception:
        pass
    time.sleep(0.05)
    try:
        win32api.SetCursorPos((cx, cy))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception:
        pass
    time.sleep(0.05)
    rect_list = [l, t, r, b]
    no_move_streak = 0
    # Electron/WebView often composites after the wheel; short sleeps yield identical
    # captures and falsely trip "at bottom" after only 2 batches while still mid-chat.
    stall_after_moved = 3
    # Never treat as "stuck at bottom" until many identical captures; avoids exiting
    # mid-chat when the first several wheel batches have not yet updated the framebuffer.
    stall_at_start = 6
    min_batches_before_stall_exit_without_move = 6
    settle_after_wheel_s = 0.14
    batches = max(1, max_batches)
    ticks = max(1, ticks_per_batch)
    seen_viewport_change = False
    for batch_idx in range(batches):
        sig_before = _image_signature_from_rect(rect_list)
        try:
            win32api.SetCursorPos((cx, cy))
        except Exception:
            pass
        for __ in range(ticks):
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -120, 0)
            time.sleep(0.025)
        time.sleep(settle_after_wheel_s)
        sig_after = _image_signature_from_rect(rect_list)
        if sig_before is None or sig_after is None:
            no_move_streak = 0
            continue
        if sig_before != sig_after:
            seen_viewport_change = True
            no_move_streak = 0
            continue
        no_move_streak += 1
        if seen_viewport_change:
            if no_move_streak >= stall_after_moved:
                break
        elif (
            batch_idx + 1 >= min_batches_before_stall_exit_without_move
            and no_move_streak >= stall_at_start
        ):
            break


def _pick_lowest_visible_icon_button(container):
    """Lowest ICON_BUTTON_CLASS under container (approx. latest assistant row actions)."""
    candidates = []
    for ctrl in _safe_descendants(container):
        try:
            info = getattr(ctrl, "element_info", None)
            class_name = (getattr(info, "class_name", "") or "").strip()
            if class_name != ICON_BUTTON_CLASS:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            if hasattr(ctrl, "is_enabled") and not ctrl.is_enabled():
                continue
            rect = _safe_rect(ctrl)
            if not rect:
                continue
            candidates.append((rect[1], ctrl))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _three_dots_scroll_params(
    settings: Optional[CursorWindowSettings],
) -> Tuple[int, int]:
    """(max_batches, ticks_per_batch) for scroll-to-bottom."""
    if settings is not None:
        return (
            max(1, settings.three_dots_scroll_to_bottom_max_batches),
            max(1, settings.three_dots_scroll_wheel_ticks_per_batch),
        )
    return (
        _three_dots_scroll_to_bottom_max_batches_from_env(48),
        _three_dots_wheel_ticks_from_env(4),
    )


def _has_three_dots_in_chat_area(
    app_window, settings: Optional[CursorWindowSettings] = None
) -> bool:
    """
    Completion signal: lowest ICON_BUTTON_CLASS under composer-messages-container
    after forcing transcript scroll to bottom.
    """
    return (
        _find_three_dots_icon_in_messages_container(app_window, settings) is not None
    )


def _find_three_dots_control(
    app_window, settings: Optional[CursorWindowSettings] = None
):
    """
    Locate the three-dots action control in chat answer area.
    """
    return _find_three_dots_icon_in_messages_container(app_window, settings)


def _find_three_dots_icon_in_messages_container(
    app_window, settings: Optional[CursorWindowSettings] = None
):
    """
    composer-messages-container 下先滚到底（视区图像稳定视为到底），再取最靠下的 ICON_BUTTON。
    """
    container = _find_composer_messages_container(app_window)
    if container is None:
        return None
    max_batches, ticks = _three_dots_scroll_params(settings)
    _scroll_messages_container_to_bottom(
        container, max_batches=max_batches, ticks_per_batch=ticks
    )
    return _pick_lowest_visible_icon_button(container)


def _find_nth_control_by_class_hint(app_window, class_hint: str, nth: int):
    """
    Find the N-th visible/enabled control whose class_name contains all hint tokens.
    nth is 1-based.
    """
    hint_tokens = [tok.strip().lower() for tok in class_hint.split() if tok.strip()]
    if not hint_tokens or nth < 1:
        return None
    matches = []
    for ctrl in _safe_descendants(app_window):
        try:
            info = getattr(ctrl, "element_info", None)
            class_name = (getattr(info, "class_name", "") or "").strip().lower()
            if not class_name:
                continue

            if not all(tok in class_name for tok in hint_tokens):
                continue

            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            if hasattr(ctrl, "is_enabled") and not ctrl.is_enabled():
                continue
            matches.append(ctrl)
        except Exception:
            continue
    if len(matches) < nth:
        return None
    return matches[nth - 1]


def _get_control_by_path(root_ctrl, path: str):
    """
    Resolve UIA node by index path like: root/1/0/0/...
    """
    if not path or not path.startswith("root"):
        return None
    indices = [p for p in path.split("/") if p and p != "root"]
    node = root_ctrl
    try:
        for idx_text in indices:
            idx = int(idx_text)
            children = node.children()
            if idx < 0 or idx >= len(children):
                return None
            node = children[idx]
        return node
    except Exception:
        return None


def _score_copy_menu_label(name: str) -> int:
    """
    Prefer full-message copy over copy-path / copy-link / copy-code.
    """
    raw = (name or "").strip()
    if not raw:
        return 0
    n = raw.lower()
    if "copy message" in n:
        return 100
    if n == "copy":
        return 90
    if "复制" in raw and "路径" not in raw and "链接" not in raw:
        return 85
    if n.startswith("copy ") and "path" not in n and "link" not in n and "code" not in n:
        return 75
    if "copy" in n and "path" not in n and "link" not in n:
        return 55
    return 0


def _find_best_copy_menu_item(root_ctrl) -> Optional[object]:
    """
    After the three-dots menu opens, locate a UIA menu row for copying assistant text.
    """
    best: Tuple[int, object] = (0, None)
    menuish = {"menuitem", "listitem", "button", "text"}
    for ctrl in _safe_descendants(root_ctrl):
        try:
            info = getattr(ctrl, "element_info", None)
            ctype = (getattr(info, "control_type", "") or "").strip().lower()
            if ctype not in menuish:
                continue
            label = (getattr(info, "name", "") or "").strip()
            score = _score_copy_menu_label(label)
            if score <= 0:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            if hasattr(ctrl, "is_enabled") and not ctrl.is_enabled():
                continue
            if score > best[0]:
                best = (score, ctrl)
        except Exception:
            continue
    return best[1]


def _find_best_copy_menu_item_foreground() -> Optional[object]:
    """
    After opening the menu, focus may move to a small popup; search only the
    foreground UIA root to avoid clicking unrelated "Copy" elsewhere on the desktop.
    """
    try:
        fg = win32gui.GetForegroundWindow()
        if not fg or not win32gui.IsWindow(fg):
            return None
        app_fg = Application(backend="uia").connect(handle=int(fg))
        root = app_fg.window(handle=int(fg))
        return _find_best_copy_menu_item(root)
    except Exception:
        return None


def _strip_markdown_json_fence(text: str) -> str:
    """
    If the model wrapped JSON in a fenced block, return inner payload only.
    Handles leading prose, ```json / ```, and trailing ```.
    """
    s = (text or "").strip()
    if "```" not in s:
        return s
    start = s.find("```")
    chunk = s[start + 3 :]
    chunk = chunk.lstrip()
    first_line, _, rest = chunk.partition("\n")
    if first_line.strip().lower() == "json":
        chunk = rest.lstrip("\r\n") if rest else ""
    chunk = chunk.lstrip("\r\n")

    end_idx = chunk.rfind("```")
    if end_idx == -1:
        return s
    return chunk[:end_idx].strip()


def _copy_message_via_three_dots_menu(
    app_window, settings: Optional[CursorWindowSettings] = None
) -> str:
    """
    Open the assistant message three-dots menu and copy full text to the clipboard.

    Do not call app_window.set_focus() after opening the menu: it steals focus from the
    popup so Down/Enter never hit the menu (user sees menu but nothing is copied).
    Prefer clicking a UIA "Copy" / "复制" menu row; fall back to Down+Enter.
    """
    dots = _find_three_dots_control(app_window, settings)
    if dots is None:
        return ""
    old_clip = pyperclip.paste()
    try:
        dots.set_focus()
        time.sleep(0.08)
        dots.click_input()
    except Exception:
        return ""

    # Let the context menu / overlay mount before we target it.
    time.sleep(0.55)

    def _read_clip_if_new() -> str:
        time.sleep(0.25)
        new_clip = pyperclip.paste()
        if new_clip and new_clip != old_clip:
            return _strip_markdown_json_fence(new_clip)
        return ""

    # 1) Direct UIA click on Copy / 复制 (main window tree).
    try:
        target = _find_best_copy_menu_item(app_window)
        if target is not None:
            target.click_input()
            got = _read_clip_if_new()
            if got:
                return got
    except Exception:
        pass

    # 2) Foreground window (menu popup may not be under the same tree as app_window).
    try:
        target = _find_best_copy_menu_item_foreground()
        if target is not None:
            target.click_input()
            got = _read_clip_if_new()
            if got:
                return got
    except Exception:
        pass

    # 3) Keyboard: keep focus on whatever owns the menu (do not focus parent window).
    try:
        send_keys("{DOWN}")
        time.sleep(0.1)
        send_keys("{ENTER}")
        got = _read_clip_if_new()
        if got:
            return got
    except Exception:
        pass

    # 4) Re-open menu and try second row (e.g. first row is Edit / Apply).
    try:
        send_keys("{ESC}")
        time.sleep(0.2)
        dots2 = _find_three_dots_control(app_window, settings)
        if dots2 is None:
            return ""
        dots2.click_input()
        time.sleep(0.55)
        send_keys("{DOWN}{DOWN}{ENTER}")
        return _read_clip_if_new()
    except Exception:
        return ""


def _extract_reply_above_three_dots(
    app_window, min_response_chars: int, settings: Optional[CursorWindowSettings] = None
) -> str:
    """
    Extract reply text from area above the three-dots control.
    """
    dots_ctrl = _find_three_dots_control(app_window, settings)
    if dots_ctrl is None:
        return ""
    dots_rect = _safe_rect(dots_ctrl)
    anchor = _find_aislash_input_control(app_window)
    anchor_rect = _safe_rect(anchor) if anchor is not None else None
    if not dots_rect or not anchor_rect:
        return ""

    dl, dt, dr, db = dots_rect
    al, at, ar, ab = anchor_rect
    aw = max(1, ar - al)
    ah = max(1, ab - at)

    # Region: directly above three-dots, aligned to answer content width.
    scope_left = al
    scope_right = dr + int(aw * 0.05)
    scope_top = at - int(ah * 14.0)
    scope_bottom = dt - int(ah * 0.25)

    blocks: List[Tuple[int, str]] = []
    for ctrl in _safe_descendants(app_window):
        try:
            txt = (ctrl.window_text() or "").strip()
            if not txt:
                continue
            rect = _safe_rect(ctrl)
            if not rect:
                continue
            cl, ct, cr, cb = rect
            cx = (cl + cr) / 2.0
            cy = (ct + cb) / 2.0
            if not (scope_left <= cx <= scope_right and scope_top <= cy <= scope_bottom):
                continue
            # Exclude obvious prompt artifacts.
            lowered = txt.lower()
            if txt in {"...", "…", "----"}:
                continue
            if "helloworld" in lowered and len(txt) <= 20:
                # keep valid tiny answer case
                pass
            if "你是测试回显助手" in txt:
                continue
            blocks.append((len(txt), txt))
        except Exception:
            continue

    if not blocks:
        return ""
    blocks.sort(key=lambda x: x[0], reverse=True)
    candidate = blocks[0][1].strip()
    return candidate if len(candidate) >= min_response_chars else ""


def _element_descriptor(ctrl) -> Dict[str, object]:
    rect = _safe_rect(ctrl)
    info = getattr(ctrl, "element_info", None)
    hwnd = None
    try:
        hwnd = getattr(info, "handle", None)
    except Exception:
        hwnd = None
    return {
        "name": (getattr(info, "name", "") or "").strip(),
        "control_type": (getattr(info, "control_type", "") or "").strip(),
        "automation_id": (getattr(info, "automation_id", "") or "").strip(),
        "class_name": (getattr(info, "class_name", "") or "").strip(),
        "hwnd": int(hwnd) if isinstance(hwnd, int) else hwnd,
        "rect": rect,
        "visible": bool(ctrl.is_visible()) if hasattr(ctrl, "is_visible") else False,
        "enabled": bool(ctrl.is_enabled()) if hasattr(ctrl, "is_enabled") else False,
    }


def enumerate_window_elements_with_paths(app_window) -> List[Dict[str, object]]:
    """
    Enumerate all descendants and attach a stable index path from root.
    path format example: root/3/12/4
    """
    rows: List[Dict[str, object]] = []
    queue: List[Tuple[object, str, int]] = [(app_window, "root", 0)]
    while queue:
        node, path, depth = queue.pop(0)
        try:
            children = node.children()
        except Exception:
            children = []
        for idx, child in enumerate(children):
            child_path = f"{path}/{idx}"
            row = _element_descriptor(child)
            row["path"] = child_path
            row["depth"] = depth + 1
            rows.append(row)
            queue.append((child, child_path, depth + 1))
    return rows


def rank_bottom_right_chat_candidates(
    app_window, elements: Optional[List[Dict[str, object]]] = None
) -> List[Dict[str, object]]:
    """
    Rank candidate chat input controls near bottom-right area.
    Returns sorted candidate descriptors with score.
    """
    rows = elements if elements is not None else enumerate_window_elements_with_paths(app_window)
    win_rect = _safe_rect(app_window)
    if not win_rect:
        return []
    wl, wt, wr, wb = win_rect
    ww = max(1, wr - wl)
    wh = max(1, wb - wt)

    ranked: List[Dict[str, object]] = []
    for row in rows:
        ctype = str(row.get("control_type", "")).lower()
        if ctype not in {"edit", "document"}:
            continue
        name = str(row.get("name", "")).lower()
        auto_id = str(row.get("automation_id", "")).lower()
        rect = row.get("rect")
        if not rect:
            continue
        cl, ct, cr, cb = rect
        cx = (cl + cr) / 2.0
        cy = (ct + cb) / 2.0
        nx = (cx - wl) / ww
        ny = (cy - wt) / wh

        score = 0.0
        # semantic hints
        if any(k in name for k in ("chat", "message", "ask", "input", "editor", "textbox")):
            score += 5.0
        if any(k in auto_id for k in ("chat", "message", "input", "editor")):
            score += 4.0
        # geometry: bottom-right preference
        if nx >= 0.60:
            score += 2.0
        if ny >= 0.55:
            score += 2.0
        # extra boost for far bottom-right
        score += max(0.0, nx - 0.55) * 2.0
        score += max(0.0, ny - 0.50) * 2.0
        if row.get("visible"):
            score += 1.0
        if row.get("enabled"):
            score += 1.0

        enriched = dict(row)
        enriched["score"] = round(score, 3)
        enriched["norm_center"] = {"x": round(nx, 4), "y": round(ny, 4)}
        ranked.append(enriched)

    ranked.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
    return ranked


def inspect_cursor_window_tree_and_chat_path(title_regex: str) -> Dict[str, object]:
    """
    Public inspection API:
    - find Cursor window handle
    - enumerate all elements with index paths
    - return ranked bottom-right chat input candidates
    """
    hwnd = _find_cursor_hwnd_with_retry(title_regex)
    app = Application(backend="uia").connect(handle=hwnd)
    window = app.window(handle=hwnd)
    elements = enumerate_window_elements_with_paths(window)
    candidates = rank_bottom_right_chat_candidates(window, elements)
    return {
        "hwnd": hwnd,
        "window_title": win32gui.GetWindowText(hwnd),
        "total_elements": len(elements),
        "elements": elements,
        "chat_candidates": candidates,
        "best_chat_candidate_path": candidates[0]["path"] if candidates else None,
    }


def find_bottom_right_button_handles(title_regex: str) -> Dict[str, object]:
    """
    Enumerate all controls and find bottom-right button handles.
    """
    hwnd = _find_cursor_hwnd_with_retry(title_regex)
    app = Application(backend="uia").connect(handle=hwnd)
    window = app.window(handle=hwnd)
    # Anchor to chat input and search nearby buttons.
    candidates = _collect_buttons_near_chat_anchor(window)
    for item in candidates:
        score = 0.0
        if item.get("visible"):
            score += 1.0
        if item.get("enabled"):
            score += 1.0
        name = str(item.get("name", "")).lower()
        klass = str(item.get("class_name", "")).lower()
        if "stop" in name:
            score += 4.0
        if any(k in name for k in ("send", "submit", "stop", "cancel")):
            score += 2.0
        if any(k in klass for k in ("codicon", "icon", "button", "spinner", "loading")):
            score += 1.0
        item["score"] = round(score, 3)

    candidates.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
    best = candidates[0] if candidates else None
    return {
        "window_hwnd": hwnd,
        "window_title": win32gui.GetWindowText(hwnd),
        "button_candidates": candidates,
        "best_button": best,
    }


def _submit_prompt_via_input_control(app_window, prompt: str):
    input_ctrl = _find_chat_input_control(app_window)
    if input_ctrl is None:
        return None

    def _read_input_text() -> str:
        try:
            # Prefer UIA value/text; fall back to window_text.
            txt = ""
            try:
                txt = str(getattr(input_ctrl, "get_value", lambda: "")() or "")
            except Exception:
                txt = ""
            if not txt:
                try:
                    txt = str(input_ctrl.window_text() or "")
                except Exception:
                    txt = ""
            return txt
        except Exception:
            return ""

    def _wait_input_non_empty(timeout_s: float = 2.0) -> bool:
        deadline = time.time() + max(0.2, float(timeout_s))
        while time.time() < deadline:
            if _read_input_text().strip():
                return True
            time.sleep(0.08)
        return False

    def _wait_input_cleared(timeout_s: float = 3.0) -> bool:
        deadline = time.time() + max(0.2, float(timeout_s))
        while time.time() < deadline:
            if not _read_input_text().strip():
                return True
            time.sleep(0.1)
        return False

    try:
        input_ctrl.set_focus()
        time.sleep(0.15)
        pyperclip.copy(prompt)
        # Clear and paste to avoid appending stale text.
        send_keys("^a")
        time.sleep(0.05)
        send_keys("^v")
        # Ensure paste actually landed before sending Enter.
        if not _wait_input_non_empty(2.2):
            # One retry: focus + paste again.
            try:
                input_ctrl.set_focus()
                time.sleep(0.08)
                send_keys("^a")
                time.sleep(0.04)
                send_keys("^v")
            except Exception:
                pass
            if not _wait_input_non_empty(1.2):
                return None

        send_keys("{ENTER}")
        # Ensure Enter sent and box cleared (Cursor sometimes keeps content if send failed).
        if not _wait_input_cleared(3.0):
            # Try once more to submit.
            send_keys("{ENTER}")
            if not _wait_input_cleared(1.5):
                return None
        return input_ctrl
    except Exception:
        return None


def _submit_prompt_fallback(prompt: str) -> None:
    pyperclip.copy(prompt)
    # Keep fallback simple and avoid extra window panel toggles.
    send_keys("^v")
    time.sleep(0.1)
    send_keys("{ENTER}")


def _extract_best_uia_answer(before: List[str], after: List[str]) -> str:
    delta = _extract_delta_text(before, after).strip()
    if len(delta) > 80:
        return delta
    long_blocks = [t for t in after if len(t.strip()) > 120 and t not in set(before)]
    if long_blocks:
        long_blocks.sort(key=len, reverse=True)
        return long_blocks[0].strip()
    return ""


def _extract_from_clipboard_with_select_all() -> str:
    """
    Fallback extraction strategy:
    select chat surface and copy all visible text, then take longest block.
    """
    old_clip = pyperclip.paste()
    send_keys("^a")
    time.sleep(0.1)
    send_keys("^c")
    time.sleep(0.2)
    copied = pyperclip.paste()
    if not copied or copied == old_clip:
        return ""
    blocks = [b.strip() for b in copied.splitlines() if len(b.strip()) > 80]
    if not blocks:
        return copied.strip()
    blocks.sort(key=len, reverse=True)
    return blocks[0]


def _is_stop_icon_active(app_window) -> bool:
    """
    Detect whether chat is still generating by checking bottom-right STOP action.
    """
    for row in _collect_buttons_near_chat_anchor(app_window):
        name = str(row.get("name", "")).lower()
        if "stop" in name:
            return True
    return False


def _wait_for_response(
    app_window,
    before: List[str],
    wait_seconds: int,
    min_response_chars: int,
    sent_prompt_markers: List[str],
    enable_clipboard_fallback: bool,
    require_three_dots_completion: bool,
    cursor_settings: Optional[CursorWindowSettings] = None,
) -> str:
    def _looks_like_prompt_echo(text: str) -> bool:
        lowered = text.lower()
        if "## output requirement" in lowered or "请仅输出最终结果内容" in lowered:
            return True
        for marker in sent_prompt_markers:
            if marker and marker.lower() in lowered:
                return True
        return False

    deadline = time.time() + wait_seconds
    last_icon_sig: Optional[str] = None
    icon_stable_rounds = 0
    while time.time() < deadline:
        time.sleep(1.0)
        # Priority 1: text STOP detection (if available).
        if _is_stop_icon_active(app_window):
            continue
        # Priority 2: chat action image dynamics.
        busy, last_icon_sig = _is_chat_action_image_busy(app_window, last_icon_sig)
        if busy:
            icon_stable_rounds = 0
            continue
        icon_stable_rounds += 1
        # Require at least one stable round before extraction.
        if icon_stable_rounds < 1:
            continue
        # Priority 3: explicit completion indicator from chat UI.
        if require_three_dots_completion and not _has_three_dots_in_chat_area(
            app_window, cursor_settings
        ):
            continue
        # Try menu-based full message copy first (exact requirement).
        copied_message = _copy_message_via_three_dots_menu(
            app_window, cursor_settings
        ).strip()
        if len(copied_message) >= min_response_chars and not _looks_like_prompt_echo(copied_message):
            return copied_message
        # Priority 4: after completion, try extracting text from above three-dots region.
        above_reply = _extract_reply_above_three_dots(
            app_window, min_response_chars, cursor_settings
        )
        if above_reply and not _looks_like_prompt_echo(above_reply):
            return above_reply

        after = _collect_text_snapshot(app_window)
        # Strategy 1: UIA delta text.
        answer = _extract_best_uia_answer(before, after)
        if len(answer.strip()) >= min_response_chars and not _looks_like_prompt_echo(answer):
            return answer
        # Strategy 2: clipboard capture fallback.
        if enable_clipboard_fallback:
            answer = _extract_from_clipboard_with_select_all().strip()
            if len(answer) >= min_response_chars and not _looks_like_prompt_echo(answer):
                return answer
    return ""


class CursorWindowClient:
    """
    Experimental Windows-only client:
    - Locate Cursor window by title regex
    - Focus chat input via Ctrl+L
    - Paste prompt and send Enter
    - Read response from UIA text delta
    """

    def __init__(
        self,
        title_regex: str,
        wait_seconds: int = 25,
        min_response_chars: int = 80,
        enable_clipboard_fallback: bool = False,
        project_root: str | None = None,
        *,
        use_disk_json_response: bool | None = None,
    ) -> None:
        pr = Path(project_root).resolve() if project_root else Path.cwd().resolve()
        disk_json = (
            _use_disk_json_response_from_env(True)
            if use_disk_json_response is None
            else use_disk_json_response
        )
        self.settings = CursorWindowSettings(
            title_regex=title_regex,
            wait_seconds=wait_seconds,
            min_response_chars=min_response_chars,
            enable_clipboard_fallback=enable_clipboard_fallback,
            use_disk_json_response=disk_json,
            disk_json_response_dir=_disk_json_response_dir_from_env(None),
        )
        self._project_root = pr

    def complete(self, system_prompt: str, user_content: str) -> str:
        hwnd = _find_cursor_hwnd_with_retry(self.settings.title_regex)
        _activate_window(hwnd)
        app = Application(backend="uia").connect(handle=hwnd)
        window = app.window(handle=hwnd)

        # Keep outgoing content faithful to the source text (no auto suffix).
        # In disk-JSON mode we send TWO queued prompts:
        # 1) main work instruction (generate/write artifacts)
        # 2) a dedicated ACK instruction to write the JSON receipt file
        combined_work = f"{system_prompt}\n\n----\n{user_content}"
        combined_ack: str | None = None
        response_path: Path | None = None
        if self.settings.use_disk_json_response:
            out_dir = (
                Path(self.settings.disk_json_response_dir).resolve()
                if self.settings.disk_json_response_dir
                else (self._project_root / ".cursor_llm" / "window_responses")
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            req_id = hashlib.md5(
                f"{time.time()}::{len(system_prompt)}::{len(user_content)}".encode("utf-8")
            ).hexdigest()[:16]
            response_path = out_dir / f"{req_id}.json"
            try:
                if response_path.exists():
                    response_path.unlink()
            except OSError:
                pass
            combined_work = (
                combined_work
                + "\n\n---\n\n"
                "Receipt will be requested in a follow-up queued instruction. "
                "For now, focus on completing the main task (writing/updating files)."
            )
            combined_ack = _cursor_disk_json_ack_prompt(response_path)
        sent_prompt_markers = [
            system_prompt[:80].strip(),
            user_content[:120].strip(),
            "----",
        ]
        window.set_focus()

        last_error: Optional[str] = None
        for _ in range(self.settings.max_retries):
            _activate_window(hwnd)
            sent_input_ctrl = _submit_prompt_via_input_control(window, combined_work)
            text_scope = (
                _closest_text_scope(sent_input_ctrl, window)
                if sent_input_ctrl is not None
                else window
            )
            before = _collect_text_snapshot(text_scope)
            if sent_input_ctrl is None:
                _submit_prompt_fallback(combined_work)
                before = _collect_text_snapshot(window)
                text_scope = window

            # Queue the ACK/receipt instruction right after the work instruction.
            if combined_ack is not None:
                try:
                    sent_ack = _submit_prompt_via_input_control(window, combined_ack)
                    if sent_ack is None:
                        _submit_prompt_fallback(combined_ack)
                except Exception:
                    pass

            if response_path is not None:
                # File-based deterministic response.
                logger.info("[cursor-window] awaiting disk JSON reply: %s", response_path)
                try:
                    raw = _wait_for_json_response_file(
                        response_path, timeout_s=self.settings.wait_seconds
                    )
                    logger.info("[cursor-window] disk JSON reply received: %s", response_path)
                    try:
                        response_path.unlink()
                    except OSError:
                        pass
                    return raw
                except TimeoutError as e:
                    # Occasionally Cursor may forget to write the JSON file. As a safety net,
                    # fall back to legacy chat scraping once to avoid hard failure.
                    last_error = str(e)
                    logger.warning("[cursor-window] disk JSON timeout, falling back to chat scrape: %s", e)
                    try:
                        scraped = _wait_for_response(
                            app_window=text_scope,
                            before=before,
                            wait_seconds=min(12, max(3, int(self.settings.wait_seconds // 4))),
                            min_response_chars=max(20, int(self.settings.min_response_chars)),
                            sent_prompt_markers=sent_prompt_markers,
                            enable_clipboard_fallback=self.settings.enable_clipboard_fallback,
                            require_three_dots_completion=self.settings.require_three_dots_completion,
                            cursor_settings=self.settings,
                        )
                        scraped = _strip_markdown_json_fence(scraped).strip()
                        if scraped:
                            # Validate it's JSON object-like; if valid, accept as success.
                            try:
                                obj = json.loads(scraped)
                            except json.JSONDecodeError:
                                obj = None
                            if isinstance(obj, dict) and "status" in obj:
                                logger.info("[cursor-window] chat-scraped JSON reply accepted")
                                # Best-effort: if file exists later, clean it.
                                try:
                                    if response_path.exists():
                                        response_path.unlink()
                                except OSError:
                                    pass
                                return scraped
                    except Exception as scrape_err:
                        logger.warning("[cursor-window] chat scrape fallback failed: %s", scrape_err)
                    continue

            # Legacy chat-scrape mode (disabled by default).
            answer = _wait_for_response(
                app_window=text_scope,
                before=before,
                wait_seconds=self.settings.wait_seconds,
                min_response_chars=self.settings.min_response_chars,
                sent_prompt_markers=sent_prompt_markers,
                enable_clipboard_fallback=self.settings.enable_clipboard_fallback,
                require_three_dots_completion=self.settings.require_three_dots_completion,
                cursor_settings=self.settings,
            )
            if answer:
                return _strip_markdown_json_fence(answer)
            last_error = "no response captured"

        raise RuntimeError(
            "Cursor window automation timed out without capturing response text. "
            "Increase GEN_CURSOR_WINDOW_WAIT_SECONDS or refine title regex. "
            f"Last error: {last_error or 'unknown'}"
        )


# -----------------------------------------------------------------------------
# Ollama HTTP client (OpenClaw LLM backend — no Cursor UI)
# -----------------------------------------------------------------------------

OllamaApiMode = Literal["native", "openai"]


@dataclass(frozen=True)
class OllamaSettings:
    """Ollama inference endpoint (OpenClaw-compatible)."""

    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:14b"
    api_mode: OllamaApiMode = "native"
    timeout_s: float = 300.0
    temperature: float = 0.2
    top_p: float = 0.9
    # When True, sets Ollama ``format: json`` (native) or response_format (OpenAI mode).
    json_mode: bool = False


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def ollama_settings_from_env() -> OllamaSettings:
    base = _env_str("OLLAMA_BASE_URL", "http://127.0.0.1:11435").rstrip("/")
    mode_raw = _env_str("OLLAMA_API_MODE", "native").lower()
    api_mode: OllamaApiMode = "openai" if mode_raw in {"openai", "openai-completions", "v1"} else "native"
    return OllamaSettings(
        base_url=base,
        model=_env_str("OLLAMA_MODEL", _env_str("OPENCLAW_OLLAMA_MODEL", "openclaw-cursor")),
        api_mode=api_mode,
        timeout_s=_env_float("OLLAMA_TIMEOUT_S", 300.0),
        temperature=_env_float("OLLAMA_TEMPERATURE", 0.2),
        json_mode=_env_bool("OLLAMA_JSON_MODE", False),
    )


def _http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
    headers: dict[str, str] | None = None,
) -> Any:
    data = None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout_s)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} {url}: {err_body[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP request failed {url}: {e}") from e
    if not raw.strip():
        return {}
    return json.loads(raw)


class OllamaLLMClient:
    """
    OpenClaw-oriented LLM client backed by Ollama.

    Same entry point as ``CursorWindowClient.complete`` for drop-in use in scripts
    that drive corpus refresh, stock resolution, etc.
    """

    def __init__(self, settings: OllamaSettings | None = None) -> None:
        self.settings = settings or ollama_settings_from_env()

    @property
    def base_url(self) -> str:
        return self.settings.base_url.rstrip("/")

    def health(self) -> bool:
        try:
            _http_json("GET", f"{self.base_url}/api/tags", timeout_s=min(10.0, self.settings.timeout_s))
            return True
        except Exception as e:
            logger.warning("[ollama] health check failed: %s", e)
            return False

    def list_models(self) -> List[str]:
        data = _http_json("GET", f"{self.base_url}/api/tags", timeout_s=min(30.0, self.settings.timeout_s))
        models = []
        for item in data.get("models") or []:
            name = item.get("name") or item.get("model")
            if name:
                models.append(str(name))
        return models

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: str | None = None,
        json_mode: bool | None = None,
    ) -> str:
        model = model or self.settings.model
        use_json = self.settings.json_mode if json_mode is None else json_mode
        if self.settings.api_mode == "openai":
            return self._chat_openai(messages, model=model, json_mode=use_json)
        return self._chat_native(messages, model=model, json_mode=use_json)

    def _chat_native(
        self,
        messages: List[Dict[str, str]],
        *,
        model: str,
        json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.settings.temperature,
                "top_p": self.settings.top_p,
            },
        }
        if json_mode:
            payload["format"] = "json"
        logger.info("[ollama] chat native model=%s messages=%d", model, len(messages))
        data = _http_json(
            "POST",
            f"{self.base_url}/api/chat",
            body=payload,
            timeout_s=self.settings.timeout_s,
        )
        msg = data.get("message") or {}
        content = str(msg.get("content") or "").strip()
        if not content:
            raise RuntimeError(f"Ollama returned empty content: {data!r}")
        return content

    def _chat_openai(
        self,
        messages: List[Dict[str, str]],
        *,
        model: str,
        json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": self.settings.temperature,
            "top_p": self.settings.top_p,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{self.base_url}/v1/chat/completions"
        api_key = _env_str("OLLAMA_API_KEY", "ollama-local")
        logger.info("[ollama] chat openai-compatible model=%s", model)
        data = _http_json(
            "POST",
            url,
            body=payload,
            timeout_s=self.settings.timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI-compatible Ollama returned no choices: {data!r}")
        content = str(((choices[0].get("message") or {}).get("content")) or "").strip()
        if not content:
            raise RuntimeError("Ollama OpenAI-compatible response empty")
        return content

    def complete(self, system_prompt: str, user_content: str) -> str:
        """Match ``CursorWindowClient.complete`` signature for OpenClaw pipelines."""
        messages: List[Dict[str, str]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": user_content.strip()})
        want_json = self.settings.json_mode or _looks_like_json_task(user_content, system_prompt)
        text = self.chat(messages, json_mode=want_json)
        return _strip_markdown_json_fence(text).strip()


def _looks_like_json_task(*parts: str) -> bool:
    blob = " ".join(parts).lower()
    return any(
        k in blob
        for k in (
            "json object",
            "valid json",
            "jsonl",
            "write the final result as a single json",
            "stock_resolutions",
            "corpus_changelog",
        )
    )


def openclaw_ollama_provider_config(
    *,
    provider_name: str = "ollama-mock",
    model_id: str | None = None,
    base_url: str | None = None,
    context_window: int = 32768,
    max_tokens: int = 8192,
) -> dict[str, Any]:
    """
    Return a fragment for ``openclaw.json`` → ``models.providers`` (merge mode).

    Example::

        cfg = openclaw_ollama_provider_config(model_id="qwen2.5:14b")
        # merge into ~/.openclaw/openclaw.json models.providers
    """
    settings = ollama_settings_from_env()
    mid = model_id or settings.model
    base = (base_url or settings.base_url).rstrip("/")
    api_mode = settings.api_mode
    api = "openai-completions" if api_mode == "openai" else "ollama"
    return {
        "models": {
            "mode": "merge",
            "providers": {
                provider_name: {
                    "baseUrl": f"{base}/v1" if api_mode == "openai" else base,
                    "apiKey": _env_str("OLLAMA_API_KEY", "ollama-local"),
                    "api": api,
                    "models": [
                        {
                            "id": mid,
                            "name": mid,
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": context_window,
                            "maxTokens": max_tokens,
                        }
                    ],
                }
            },
        },
        "agents": {
            "defaults": {
                "model": {"primary": f"{provider_name}/{mid}"},
            }
        },
    }


OpenClawLLMBackend = Literal["ollama", "cursor"]


@dataclass
class OpenClawLLMSettings:
    backend: OpenClawLLMBackend = "ollama"


def openclaw_llm_backend_from_env(default: OpenClawLLMBackend = "ollama") -> OpenClawLLMBackend:
    raw = _env_str("OPENCLAW_LLM_BACKEND", default).lower()
    if raw in {"cursor", "cursor-window", "window"}:
        return "cursor"
    return "ollama"


class OpenClawLLMClient:
    """
    Unified LLM facade for OpenClaw stage-2 tasks (corpus refresh, clue resolution, …).

    Delegates to ``OllamaLLMClient`` or ``CursorWindowClient`` based on ``backend``.
    """

    def __init__(
        self,
        backend: OpenClawLLMBackend | None = None,
        *,
        ollama: OllamaSettings | None = None,
        cursor_title_regex: str | None = None,
        cursor_wait_seconds: int | None = None,
        cursor_project_root: str | None = None,
    ) -> None:
        self.backend = backend or openclaw_llm_backend_from_env()
        self._ollama: Optional[OllamaLLMClient] = None
        self._cursor: Optional[CursorWindowClient] = None
        if self.backend == "ollama":
            self._ollama = OllamaLLMClient(ollama)
        else:
            title = cursor_title_regex or _env_str("GEN_CURSOR_WINDOW_TITLE_REGEX", ".*Cursor.*")
            wait = cursor_wait_seconds
            if wait is None:
                wait = int(_env_float("GEN_CURSOR_WINDOW_WAIT_SECONDS", 120))
            self._cursor = CursorWindowClient(
                title_regex=title,
                wait_seconds=wait,
                project_root=cursor_project_root,
            )

    def complete(self, system_prompt: str, user_content: str) -> str:
        if self._ollama is not None:
            return self._ollama.complete(system_prompt, user_content)
        assert self._cursor is not None
        return self._cursor.complete(system_prompt, user_content)

    def health(self) -> bool:
        if self._ollama is not None:
            return self._ollama.health()
        try:
            title = _env_str("GEN_CURSOR_WINDOW_TITLE_REGEX", ".*Cursor.*")
            _find_cursor_hwnd_with_retry(title, timeout_s=3.0)
            return True
        except Exception:
            return False


def create_openclaw_llm_client(**kwargs: Any) -> OpenClawLLMClient:
    """Factory: ``create_openclaw_llm_client()`` → backend from ``OPENCLAW_LLM_BACKEND``."""
    return OpenClawLLMClient(**kwargs)

