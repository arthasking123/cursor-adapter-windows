# Cursor Adapter (Windows) — OpenAI Compatible

该工程将 **Cursor 桌面端（Windows）** 作为“模型提供方”，通过 UI 自动化把 OpenAI 标准请求转发到 Cursor Chat，并在本机暴露 **OpenAI 兼容 API**（默认 `localhost:17325`）。

## 能做什么

- 提供 OpenAI 兼容接口：
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- 后端实现基于 `pywinauto` + `pywin32` + UIA，对 Cursor 窗口进行自动化输入与抓取回复

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

## 运行（监听 localhost:17325）

```powershell
$env:CURSOR_WINDOW_TITLE_REGEX=".*Cursor.*"
$env:CURSOR_WINDOW_WAIT_SECONDS="35"
$env:CURSOR_WINDOW_MIN_RESPONSE_CHARS="1"
python -m cursor_adapter.server.app
```


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

- `CURSOR_ADAPTER_HOST`：默认 `127.0.0.1`
- `CURSOR_ADAPTER_PORT`：默认 `17325`
- `CURSOR_WINDOW_TITLE_REGEX`：默认 `.*Cursor.*`
- `CURSOR_WINDOW_WAIT_SECONDS`：默认 `35`
- `CURSOR_WINDOW_MIN_RESPONSE_CHARS`：默认 `80`
- `CURSOR_WINDOW_CLIPBOARD_FALLBACK`：默认 `0`（设为 `1` 启用）

## 注意事项

- UI 自动化对 Cursor 版本/布局/焦点较敏感；建议运行时把 Cursor 置前台、并保持 Chat 面板可输入。
- 该服务不会计算真实 token，`usage` 字段会返回 0。

