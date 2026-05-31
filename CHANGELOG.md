# Changelog

## 0.2.0 — 2026-05-31

- 合并 JFCY-skills `cursor_adapter` 完整实现（`cursor_window_client.py` ~1864 行）
- 新增模拟 Ollama API：`cursor_adapter.server.ollama_mock`（默认 `127.0.0.1:11435`）
- OpenAI 兼容服务保留 FastAPI（默认 `127.0.0.1:17325`）
- CLI：`cursor-adapter-ollama-mock`、`cursor-adapter-openai`
- 环境变量别名：`GEN_CURSOR_WINDOW_*` 与 `CURSOR_WINDOW_*` 双读
- 新增 `run_ollama_mock.ps1`

## 0.1.0

- 初始 OpenAI 兼容 API + 基础 UIA 自动化
