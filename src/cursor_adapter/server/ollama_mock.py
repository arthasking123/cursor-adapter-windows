"""
模拟 Ollama HTTP API（不依赖、不启动真实 ``ollama serve``）。

供 OpenClaw 等连接 ``http://127.0.0.1:11435``；推理可转发 Cursor 窗口或使用 echo 回显。

环境变量：
  OLLAMA_MOCK_HOST / OLLAMA_MOCK_PORT / OLLAMA_BASE_URL
  OLLAMA_MOCK_MODEL / OLLAMA_MOCK_BACKEND (echo|cursor)
  CURSOR_WINDOW_TITLE_REGEX / GEN_CURSOR_WINDOW_TITLE_REGEX
  CURSOR_WINDOW_WAIT_SECONDS / GEN_CURSOR_WINDOW_WAIT_SECONDS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from ..adapters.cursor_window_client import CursorWindowClient, OllamaLLMClient, OllamaSettings

logger = logging.getLogger(__name__)

MockBackend = Literal["echo", "cursor"]


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    for key in (name,):
        raw = _env_str(key)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return default


def _parse_base_url(url: str) -> Tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, int(port)


@dataclass
class OllamaMockConfig:
    host: str = "127.0.0.1"
    port: int = 11435
    model: str = "openclaw-cursor"
    backend: MockBackend = "echo"
    startup_timeout_s: float = 15.0
    cursor_title_regex: str = ".*Cursor.*"
    cursor_wait_seconds: int = 120


def ollama_mock_config_from_env() -> OllamaMockConfig:
    base = _env_str("OLLAMA_BASE_URL")
    host = _env_str("OLLAMA_MOCK_HOST", "127.0.0.1")
    port = 11435
    if base:
        host, port = _parse_base_url(base)
    else:
        port_raw = _env_str("OLLAMA_MOCK_PORT")
        if port_raw:
            port = int(port_raw)
    backend_raw = _env_str("OLLAMA_MOCK_BACKEND", "echo").lower()
    backend: MockBackend = "cursor" if backend_raw == "cursor" else "echo"
    title = _env_str("CURSOR_WINDOW_TITLE_REGEX") or _env_str(
        "GEN_CURSOR_WINDOW_TITLE_REGEX", ".*Cursor.*"
    )
    wait = _env_int("CURSOR_WINDOW_WAIT_SECONDS", 0) or _env_int(
        "GEN_CURSOR_WINDOW_WAIT_SECONDS", 120
    )
    return OllamaMockConfig(
        host=host,
        port=port,
        model=_env_str("OLLAMA_MOCK_MODEL", _env_str("OLLAMA_MODEL", "openclaw-cursor")),
        backend=backend,
        cursor_title_regex=title,
        cursor_wait_seconds=wait,
    )


def _messages_to_system_user(messages: List[Dict[str, Any]]) -> Tuple[str, str]:
    system_parts: List[str] = []
    user_parts: List[str] = []
    for m in messages:
        role = str(m.get("role") or "user").lower()
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            user_parts.append(f"[assistant]\n{content}")
        else:
            user_parts.append(content)
    return "\n\n".join(system_parts), "\n\n".join(user_parts)


def _want_json(body: dict[str, Any], user_text: str, system_text: str) -> bool:
    if body.get("format") == "json":
        return True
    rf = body.get("response_format") or {}
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        return True
    blob = f"{system_text}\n{user_text}".lower()
    return any(k in blob for k in ("json object", "valid json", "jsonl", '"status"'))


class _MockOllamaRouter:
    def __init__(self, config: OllamaMockConfig) -> None:
        self.config = config
        self._cursor: Optional[CursorWindowClient] = None

    def _cursor_client(self) -> CursorWindowClient:
        if self._cursor is None:
            self._cursor = CursorWindowClient(
                title_regex=self.config.cursor_title_regex,
                wait_seconds=self.config.cursor_wait_seconds,
                use_disk_json_response=False,
            )
        return self._cursor

    def complete_chat(self, body: dict[str, Any]) -> str:
        messages = body.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        system_text, user_text = _messages_to_system_user(messages)
        model = str(body.get("model") or self.config.model)
        json_mode = _want_json(body, user_text, system_text)

        if self.config.backend == "cursor":
            logger.info("[ollama-mock] cursor backend model=%s", model)
            return self._cursor_client().complete(system_text, user_text).strip()

        logger.info("[ollama-mock] echo backend model=%s", model)
        if json_mode:
            return json.dumps(
                {"status": "ok", "engine": "mock-ollama", "model": model},
                ensure_ascii=False,
            )
        upper = user_text.upper()
        if "PONG" in upper or "EXACTLY: PONG" in upper:
            return "PONG"
        if user_text.strip():
            return f"[mock-ollama/{model}] {user_text.strip()[:500]}"
        return f"[mock-ollama/{model}] ok"


def _make_handler(router: _MockOllamaRouter, models: List[str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("[ollama-mock] " + fmt, *args)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if not raw.strip():
                return {}
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/api/version"):
                self._send_json(
                    200,
                    {"version": "mock-ollama/1.0", "engine": router.config.backend},
                )
                return
            if path == "/api/tags":
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._send_json(
                    200,
                    {
                        "models": [
                            {
                                "name": m,
                                "model": m,
                                "modified_at": now,
                                "size": 0,
                                "digest": "mock",
                                "details": {"family": "mock", "parameter_size": "0"},
                            }
                            for m in models
                        ]
                    },
                )
                return
            self._send_json(404, {"error": f"not found: {path}"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"invalid json: {e}"})
                return
            if path == "/api/chat":
                self._handle_api_chat(body)
                return
            if path == "/v1/chat/completions":
                self._handle_openai_chat(body)
                return
            self._send_json(404, {"error": f"not found: {path}"})

        def _handle_api_chat(self, body: dict[str, Any]) -> None:
            model = str(body.get("model") or router.config.model)
            stream = bool(body.get("stream"))
            try:
                content = router.complete_chat(body)
            except Exception as e:
                logger.exception("[ollama-mock] chat failed")
                self._send_json(500, {"error": str(e)})
                return
            if stream:
                self._stream_native(model, content)
                return
            self._send_json(
                200,
                {
                    "model": model,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                    "done_reason": "stop",
                },
            )

        def _stream_native(self, model: str, content: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            chunk = {
                "model": model,
                "message": {"role": "assistant", "content": content},
                "done": False,
            }
            self.wfile.write((json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8"))
            done = {
                "model": model,
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
            }
            self.wfile.write((json.dumps(done, ensure_ascii=False) + "\n").encode("utf-8"))

        def _handle_openai_chat(self, body: dict[str, Any]) -> None:
            model = str(body.get("model") or router.config.model)
            stream = bool(body.get("stream"))
            ollama_body = {
                "model": model,
                "messages": body.get("messages") or [],
                "stream": False,
                "response_format": body.get("response_format"),
            }
            try:
                content = router.complete_chat(ollama_body)
            except Exception as e:
                self._send_json(500, {"error": {"message": str(e), "type": "server_error"}})
                return
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                rid = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
                payload = {
                    "id": rid,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                return
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    return Handler


@dataclass
class OllamaMockService:
    config: OllamaMockConfig = field(default_factory=ollama_mock_config_from_env)
    _server: Optional[ThreadingHTTPServer] = field(default=None, init=False, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _started_by_us: bool = field(default=False, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    def llm_settings(self) -> OllamaSettings:
        return OllamaSettings(base_url=self.base_url, model=self.config.model)

    def llm_client(self) -> OllamaLLMClient:
        return OllamaLLMClient(self.llm_settings())

    def is_healthy(self) -> bool:
        return self.llm_client().health()

    def start(self, *, background: bool = True) -> None:
        if self.is_healthy():
            logger.info("[ollama-mock] already listening at %s", self.base_url)
            return
        router = _MockOllamaRouter(self.config)
        handler = _make_handler(router, [self.config.model])
        self._server = ThreadingHTTPServer((self.config.host, self.config.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ollama-mock-http",
            daemon=True,
        )
        self._thread.start()
        self._started_by_us = True
        logger.info(
            "[ollama-mock] serving %s backend=%s model=%s",
            self.base_url,
            self.config.backend,
            self.config.model,
        )
        deadline = time.time() + self.config.startup_timeout_s
        while time.time() < deadline:
            if self.is_healthy():
                return
            time.sleep(0.15)
        self.stop()
        raise TimeoutError(f"模拟 Ollama API 启动超时: {self.base_url}")

    def stop(self, *, only_if_started_by_us: bool = True) -> None:
        if only_if_started_by_us and not self._started_by_us:
            return
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._started_by_us = False

    def __enter__(self) -> "OllamaMockService":
        self.start(background=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop(only_if_started_by_us=True)

    def run_forever(self) -> None:
        self.start(background=True)
        print(
            f"mock Ollama API 已监听 {self.base_url} "
            f"(backend={self.config.backend}, model={self.config.model})",
            flush=True,
        )
        print("按 Ctrl+C 停止…", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n正在关闭…", flush=True)
        finally:
            self.stop(only_if_started_by_us=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="常驻运行模拟 Ollama HTTP API（默认 127.0.0.1:11435）",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--backend", choices=("echo", "cursor"), default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = ollama_mock_config_from_env()
    if args.host:
        cfg = OllamaMockConfig(**{**cfg.__dict__, "host": args.host})
    if args.port is not None:
        cfg = OllamaMockConfig(**{**cfg.__dict__, "port": args.port})
    if args.backend:
        cfg = OllamaMockConfig(**{**cfg.__dict__, "backend": args.backend})  # type: ignore[arg-type]
    if args.model:
        cfg = OllamaMockConfig(**{**cfg.__dict__, "model": args.model})

    try:
        OllamaMockService(cfg).run_forever()
    except TimeoutError as e:
        logger.error("%s", e)
        return 1
    except OSError as e:
        logger.error("无法绑定端口 %s:%s — %s", cfg.host, cfg.port, e)
        return 1
    return 0


def ensure_mock_ollama(
    *,
    backend: MockBackend | None = None,
    model: str | None = None,
    port: int | None = None,
) -> OllamaMockService:
    """启动模拟 API 并返回服务实例（不自动 stop）。"""
    cfg = ollama_mock_config_from_env()
    if backend is not None:
        cfg = OllamaMockConfig(
            host=cfg.host,
            port=cfg.port,
            model=cfg.model,
            backend=backend,
            startup_timeout_s=cfg.startup_timeout_s,
            cursor_title_regex=cfg.cursor_title_regex,
            cursor_wait_seconds=cfg.cursor_wait_seconds,
        )
    if model:
        cfg = OllamaMockConfig(**{**cfg.__dict__, "model": model})
    if port is not None:
        cfg = OllamaMockConfig(**{**cfg.__dict__, "port": port})
    svc = OllamaMockService(cfg)
    svc.start(background=True)
    return svc


if __name__ == "__main__":
    raise SystemExit(main())
