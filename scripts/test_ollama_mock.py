#!/usr/bin/env python3
"""
测试「模拟 Ollama HTTP API」+ OllamaLLMClient / OpenClaw 配置片段。

不安装、不启动真实 ollama serve。

用法（在 cursor_adapter 目录或项目根）::

    python cursor_adapter/test_ollama_llm.py
    python cursor_adapter/test_ollama_llm.py --backend echo
    python cursor_adapter/test_ollama_llm.py --backend cursor   # 转发到 Cursor 窗口
    python cursor_adapter/test_ollama_llm.py --port 11435 --no-stop
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cursor_adapter.adapters.cursor_window_client import (  # noqa: E402
    OllamaLLMClient,
    openclaw_ollama_provider_config,
)
from cursor_adapter.server.ollama_mock import OllamaMockConfig, OllamaMockService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("test_ollama_mock")


def _step(name: str, ok: bool, detail: str = "") -> int:
    mark = "OK" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Test mock Ollama HTTP API")
    parser.add_argument("--model", default=None, help="对外模型名")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 11435")
    parser.add_argument(
        "--backend",
        choices=("echo", "cursor"),
        default=os.environ.get("OLLAMA_MOCK_BACKEND", "echo"),
        help="echo=内置回显；cursor=转发 CursorWindowClient",
    )
    parser.add_argument("--health-only", action="store_true", help="只测 /api/tags")
    parser.add_argument("--no-serve", action="store_true", help="假定 mock 已在运行")
    parser.add_argument("--no-stop", action="store_true", help="结束后不关闭 mock 服务")
    args = parser.parse_args()

    exit_code = 0
    model = args.model or os.environ.get("OLLAMA_MOCK_MODEL", "openclaw-cursor")
    port = args.port if args.port is not None else int(os.environ.get("OLLAMA_MOCK_PORT", "11435"))

    cfg = OllamaMockConfig(
        port=port,
        model=model,
        backend=args.backend,  # type: ignore[arg-type]
    )
    mock = OllamaMockService(cfg)

    try:
        if not args.no_serve:
            try:
                mock.start()
                exit_code |= _step("start mock ollama api", mock.is_healthy(), mock.base_url)
            except Exception as e:
                exit_code |= _step("start mock ollama api", False, str(e))
                return exit_code
        else:
            exit_code |= _step(
                "mock already running",
                mock.is_healthy(),
                f"需已有服务监听 {mock.base_url}",
            )

        client = OllamaLLMClient(mock.llm_settings())
        models = client.list_models()
        exit_code |= _step("GET /api/tags", bool(models), ", ".join(models))

        if args.health_only:
            os.environ["OLLAMA_BASE_URL"] = mock.base_url
            snippet = openclaw_ollama_provider_config(model_id=model)
            print("\nOpenClaw provider snippet:")
            print(json.dumps(snippet["agents"]["defaults"]["model"], ensure_ascii=False, indent=2))
            print(f"\nOLLAMA_BASE_URL={mock.base_url}")
            return exit_code

        if args.backend == "echo":
            reply = client.complete(
                "你是简短助手。",
                "请回复 exactly: PONG",
            )
            exit_code |= _step("POST /api/chat (PONG)", "PONG" in reply.upper(), reply[:120])

            reply_json = client.complete(
                "输出 JSON。",
                '写 JSON {"status":"ok","engine":"ollama"}',
            )
            try:
                obj = json.loads(reply_json)
                exit_code |= _step(
                    "POST /api/chat (json)",
                    obj.get("status") == "ok",
                    reply_json[:120],
                )
            except json.JSONDecodeError as e:
                exit_code |= _step("POST /api/chat (json)", False, str(e))
        else:
            log.info("cursor 后端：需已打开 Cursor 窗口，可能较慢…")
            reply = client.complete("你是测试助手。", "用一句话说你好。")
            exit_code |= _step("POST /api/chat (cursor)", len(reply.strip()) > 0, reply[:120])

    finally:
        if not args.no_stop:
            mock.stop()
        else:
            print(f"[info] mock 保持运行: {mock.base_url}")

    print("\n" + ("全部通过" if exit_code == 0 else "存在失败项"))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
