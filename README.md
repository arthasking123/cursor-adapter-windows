# Cursor Adapter (Windows)

将 **Cursor 桌面端（Windows）** 通过 UI 自动化暴露为本地 HTTP API，支持两种协议：

| 服务 | 默认端口 | 用途 |
|------|----------|------|
| **OpenAI 兼容** | `17325` | 通用 `POST /v1/chat/completions` |
| **Ollama 模拟** | `11435` | OpenClaw `ollama-mock/openclaw-cursor` 等 |

核心实现：`src/cursor_adapter/adapters/cursor_window_client.py`（UIA + 可选磁盘 JSON 回执）。

## 能做什么

- OpenAI 兼容：`GET /v1/models`、`POST /v1/chat/completions`（FastAPI @ 17325）
- Ollama 模拟：`GET /api/tags`、`POST /api/chat`、亦支持 `/v1/chat/completions`（@ 11435）
- 后端：`pywinauto` + `pywin32` + UIA，驱动 Cursor Chat 输入并抓取回复

## 可使用的场景

- **把“只支持 OpenAI 接口”的工具接到 Cursor**：例如本地脚本、内部服务、评测/对话压测工具，只要会调用 `POST /v1/chat/completions` 就能无改造或少改造接入。
- **内网/受限网络环境的临时桥接**：当你无法直接访问外部模型服务，但本机可以正常使用 Cursor Chat 时，可用它把调用统一收敛到本机的 OpenAI 兼容地址。
- **做 PoC / 集成联调**：前端/后端先按 OpenAI 协议打通端到端链路（鉴权、超时、重试、错误处理、日志等），后续再替换为真实模型网关或自建推理服务。
- **回归与可重复测试**：用固定 prompts（如 `scripts/test_openai_chat.ps1`）在升级 Cursor、调整 UI 布局或修改自动化逻辑后快速验证“能否稳定拿到回复”。
- **本地代理给多语言客户端**：把 Python/Node/Go 等不同客户端统一指向 `localhost:17325`，降低每个客户端各自适配 Cursor 的成本。

## 环境要求

- Windows 10/11
- Python 3.10+
- 已安装并登录 Cursor，且 Chat 面板可输入

## 安装

```powershell
cd cursor-adapter-windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

## 运行 OpenAI 服务（17325）

```powershell
$env:CURSOR_WINDOW_TITLE_REGEX=".*Cursor.*"
$env:CURSOR_WINDOW_WAIT_SECONDS="35"
$env:CURSOR_WINDOW_MIN_RESPONSE_CHARS="1"
python -m cursor_adapter.server.app
```

或：`.\run_server.ps1` / `cursor-adapter-openai`

## 运行 Ollama 模拟（11435，OpenClaw）

```powershell
$env:OLLAMA_MOCK_BACKEND="cursor"
$env:CURSOR_WINDOW_WAIT_SECONDS="120"
python -m cursor_adapter.server.ollama_mock --backend cursor
```

或：`.\run_ollama_mock.ps1` / `cursor-adapter-ollama-mock --backend cursor`


如果你不想安装 editable，也可以用（等价）方式临时指定 `PYTHONPATH`：

```powershell
$env:PYTHONPATH="src"
python -m cursor_adapter.server.app
```

更推荐直接用一键脚本（会优先使用本目录下 `.venv` 的 Python）：

```powershell
.\run_server.ps1
```

## OpenAI Chat 测试脚本

确保服务已启动后执行：

```powershell
.\scripts\test_openai_chat.ps1
```

启动后：

- `GET http://localhost:17325/health`
- `GET http://localhost:17325/v1/models`
- `POST http://localhost:17325/v1/chat/completions`

## cURL 示例

```bash
curl http://localhost:17325/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"cursor-window\",\"messages\":[{\"role\":\"system\",\"content\":\"You are a helpful assistant.\"},{\"role\":\"user\",\"content\":\"Say HELLOWORLD\"}]}"
```

## 配置项（环境变量）

**OpenAI 服务**

- `CURSOR_ADAPTER_HOST` / `CURSOR_ADAPTER_PORT`：默认 `127.0.0.1:17325`
- `CURSOR_WINDOW_TITLE_REGEX`（或 `GEN_CURSOR_WINDOW_TITLE_REGEX`）
- `CURSOR_WINDOW_WAIT_SECONDS`（或 `GEN_CURSOR_WINDOW_WAIT_SECONDS`）
- `CURSOR_WINDOW_MIN_RESPONSE_CHARS`、`CURSOR_WINDOW_CLIPBOARD_FALLBACK`
- `GEN_CURSOR_WINDOW_USE_DISK_JSON`：默认 `0`（OpenAI 路径建议关闭）

**Ollama 模拟**

- `OLLAMA_MOCK_HOST` / `OLLAMA_MOCK_PORT`：默认 `127.0.0.1:11435`
- `OLLAMA_MOCK_BACKEND`：`echo` | `cursor`
- `OLLAMA_MOCK_MODEL`：默认 `openclaw-cursor`

## OpenClaw 集成

```json
"baseUrl": "http://127.0.0.1:11435",
"model": "openclaw-cursor"
```

常驻：`.\run_ollama_mock.ps1` 或 `cursor-adapter-ollama-mock --backend cursor`

## 注意事项

- UI 自动化对 Cursor 版本/布局/焦点较敏感；建议运行时把 Cursor 置前台、并保持 Chat 面板可输入。
- 该服务不会计算真实 token，`usage` 字段会返回 0。

