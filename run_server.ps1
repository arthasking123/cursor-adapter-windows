Param(
  [string]$HostAddr = "127.0.0.1",
  [int]$Port = 17325,
  [string]$TitleRegex = ".*Cursor.*",
  [int]$WaitSeconds = 35,
  [int]$MinChars = 1,
  [switch]$ClipboardFallback
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  $pythonExe = $venvPython
} else {
  Write-Warning "未检测到 .venv，将使用当前 PATH 中的 python（可能导致 FastAPI/Pydantic 版本冲突）。"
  $pythonExe = "python"
}

$env:CURSOR_ADAPTER_HOST = $HostAddr
$env:CURSOR_ADAPTER_PORT = "$Port"
$env:CURSOR_WINDOW_TITLE_REGEX = $TitleRegex
$env:CURSOR_WINDOW_WAIT_SECONDS = "$WaitSeconds"
$env:CURSOR_WINDOW_MIN_RESPONSE_CHARS = "$MinChars"
$env:CURSOR_WINDOW_CLIPBOARD_FALLBACK = ($(if ($ClipboardFallback) { "1" } else { "0" }))

# Ensure src-layout imports work without install.
$env:PYTHONPATH = "src"

& $pythonExe -m cursor_adapter.server.app

