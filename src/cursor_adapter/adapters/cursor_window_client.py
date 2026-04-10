from __future__ import annotations

import difflib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pyperclip
import win32con
import win32gui
from PIL import ImageGrab
from pywinauto import Application
from pywinauto.keyboard import send_keys

MESSAGE_CONTAINER_CLASS = "composer-messages-container"
ICON_BUTTON_CLASS = (
    "anysphere-icon-button bg-[transparent] border-none text-[var(--cursor-text-primary)] "
    "flex w-4 items-center justify-center"
)

logger = logging.getLogger(__name__)


def _prompt_fingerprint(text: str) -> str:
    """
    Short, non-reversible fingerprint for logging correlation.
    """
    try:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class CursorWindowSettings:
    title_regex: str
    wait_seconds: int
    min_response_chars: int = 80
    max_retries: int = 2
    enable_clipboard_fallback: bool = False
    require_three_dots_completion: bool = True


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


def _activate_window(hwnd: int) -> None:
    # Do not change window size/state, only bring it to foreground.
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.25)


def _collect_text_snapshot(app_window) -> List[str]:
    texts: List[str] = []
    for ctrl in app_window.descendants():
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
    for ctrl in app_window.descendants():
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
    for ctrl in app_window.descendants():
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
    for ctrl in app_window.descendants():
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
    for ctrl in app_window.descendants():
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
    for ctrl in app_window.descendants():
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


def _has_three_dots_in_chat_area(app_window) -> bool:
    """
    Completion signal: ICON 2 under composer-messages-container.
    """
    return _find_three_dots_icon_in_messages_container(app_window) is not None


def _find_three_dots_control(app_window):
    """
    Locate the three-dots action control in chat answer area.
    """
    return _find_three_dots_icon_in_messages_container(app_window)


def _find_three_dots_icon_in_messages_container(app_window):
    """
    Implements:
    composer-messages-container 下的 ICON_BUTTON_CLASS 取第 2 个（更靠下的那个），点击它作为三个点。
    这里用 rect.top 最大者近似 ICON 2（更靠下）。
    """
    container = None
    for ctrl in app_window.descendants():
        try:
            info = getattr(ctrl, "element_info", None)
            class_name = (getattr(info, "class_name", "") or "").strip()
            if class_name != MESSAGE_CONTAINER_CLASS:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            container = ctrl
            break
        except Exception:
            continue

    if container is None:
        return None

    candidates = []
    for ctrl in container.descendants():
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

    if len(candidates) < 1:
        return None

    # "ICON 2" => the lower one
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

def _select_second_menu_item_near_dots(app_window, dots_rect: List[int]) -> bool:
    """
    After clicking three-dots, choose the second visible menu item near popup.
    """
    dl, dt, dr, db = dots_rect
    # Typical popup is below/near the dots icon.
    search_left = dl - 220
    search_right = dr + 260
    search_top = dt - 20
    search_bottom = db + 320

    rows: List[Tuple[float, object]] = []
    for ctrl in app_window.descendants():
        try:
            info = getattr(ctrl, "element_info", None)
            ctype = (getattr(info, "control_type", "") or "").strip().lower()
            if ctype not in {"menuitem", "listitem", "button", "text"}:
                continue
            if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
                continue
            rect = _safe_rect(ctrl)
            if not rect:
                continue
            cl, ct, cr, cb = rect
            cx = (cl + cr) / 2.0
            cy = (ct + cb) / 2.0
            if not (search_left <= cx <= search_right and search_top <= cy <= search_bottom):
                continue
            if (cr - cl) < 40 or (cb - ct) < 12:
                continue
            rows.append((cy, ctrl))
        except Exception:
            continue

    if len(rows) < 2:
        return False
    rows.sort(key=lambda x: x[0])
    second = rows[1][1]
    try:
        second.set_focus()
        second.click_input()
        return True
    except Exception:
        return False


def _copy_message_via_three_dots_menu(app_window) -> str:
    """
    Click three-dots -> choose second menu item -> read clipboard content.
    """
    dots = _find_three_dots_control(app_window)
    if dots is None:
        return ""
    rect = _safe_rect(dots)
    if not rect:
        return ""
    old_clip = pyperclip.paste()
    try:
        dots.set_focus()
        dots.click_input()
    except Exception:
        return ""

    time.sleep(0.35)
    ok = _select_second_menu_item_near_dots(app_window, list(rect))
    if not ok:
        # Try keyboard fallback: second item then enter.
        send_keys("{DOWN}")
        time.sleep(0.05)
        send_keys("{ENTER}")
    time.sleep(0.35)
    new_clip = pyperclip.paste()
    if new_clip and new_clip != old_clip:
        return new_clip.strip()
    return ""


def _extract_reply_above_three_dots(app_window, min_response_chars: int) -> str:
    """
    Extract reply text from area above the three-dots control.
    """
    dots_ctrl = _find_three_dots_control(app_window)
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
    for ctrl in app_window.descendants():
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

def _submit_prompt_via_input_control(app_window, prompt: str):
    input_ctrl = _find_chat_input_control(app_window)
    if input_ctrl is None:
        return None

    try:
        input_ctrl.set_focus()
        time.sleep(0.15)
        pyperclip.copy(prompt)
        # Clear and paste to avoid appending stale text.
        send_keys("^a")
        time.sleep(0.05)
        send_keys("^v")
        time.sleep(0.1)
        send_keys("{ENTER}")
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
) -> str:
    def _looks_like_prompt_echo(text: str) -> bool:
        lowered = text.lower()
        if "## output requirement" in lowered or "请仅输出最终结果内容" in lowered:
            return True
        logger.debug("sent_prompt_markers=%s", sent_prompt_markers)
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
        if require_three_dots_completion and not _has_three_dots_in_chat_area(app_window):
            continue
        # Try menu-based full message copy first (exact requirement).
        copied_message = _copy_message_via_three_dots_menu(app_window).strip()
        if len(copied_message) >= min_response_chars and not _looks_like_prompt_echo(copied_message):
            return copied_message
        # Priority 4: after completion, try extracting text from above three-dots region.
        above_reply = _extract_reply_above_three_dots(app_window, min_response_chars)
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
    ) -> None:
        self.settings = CursorWindowSettings(
            title_regex=title_regex,
            wait_seconds=wait_seconds,
            min_response_chars=min_response_chars,
            enable_clipboard_fallback=enable_clipboard_fallback,
        )

    def complete(self, system_prompt: str, user_content: str) -> str:
        prompt_sig = _prompt_fingerprint(system_prompt + "\n\n----\n" + user_content)
        hwnd = _find_cursor_hwnd(self.settings.title_regex)
        _activate_window(hwnd)
        app = Application(backend="uia").connect(handle=hwnd)
        window = app.window(handle=hwnd)

        # Keep outgoing content faithful to the source text (no auto suffix).
        combined = f"{system_prompt}\n\n----\n{user_content}"
        sent_prompt_markers = [
            system_prompt[:80].strip(),
            user_content[:120].strip(),
            "----",
        ]
        window.set_focus()

        last_error: Optional[str] = None
        for attempt in range(self.settings.max_retries):
            try:
                _activate_window(hwnd)
                sent_input_ctrl = _submit_prompt_via_input_control(window, combined)
                text_scope = (
                    _closest_text_scope(sent_input_ctrl, window)
                    if sent_input_ctrl is not None
                    else window
                )
                before = _collect_text_snapshot(text_scope)
                if sent_input_ctrl is None:
                    _submit_prompt_fallback(combined)
                    before = _collect_text_snapshot(window)
                    text_scope = window

                answer = _wait_for_response(
                    app_window=text_scope,
                    before=before,
                    wait_seconds=self.settings.wait_seconds,
                    min_response_chars=self.settings.min_response_chars,
                    sent_prompt_markers=sent_prompt_markers,
                    enable_clipboard_fallback=self.settings.enable_clipboard_fallback,
                    require_three_dots_completion=self.settings.require_three_dots_completion,
                )
                if answer:
                    return answer
                last_error = "no response captured"
                logger.warning(
                    "CursorWindowClient.complete: empty answer (attempt=%s/%s, prompt_sig=%s, title_regex=%s, wait_seconds=%s, min_chars=%s)",
                    attempt + 1,
                    self.settings.max_retries,
                    prompt_sig,
                    self.settings.title_regex,
                    self.settings.wait_seconds,
                    self.settings.min_response_chars,
                )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.exception(
                    "CursorWindowClient.complete failed (attempt=%s/%s, prompt_sig=%s, title_regex=%s)",
                    attempt + 1,
                    self.settings.max_retries,
                    prompt_sig,
                    self.settings.title_regex,
                )

        raise RuntimeError(
            "Cursor window automation timed out without capturing response text. "
            "Increase CURSOR_WINDOW_WAIT_SECONDS or refine title regex. "
            f"Last error: {last_error or 'unknown'}"
        )

