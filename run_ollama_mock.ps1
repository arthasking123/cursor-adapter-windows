Param(
  [string]$HostAddr = "127.0.0.1",
  [int]$Port = 11435,
  [ValidateSet("echo", "cursor")]
  [string]$Backend = "cursor",
  [string]$Model = "openclaw-cursor",
  [string]$TitleRegex = ".*Cursor.*",
  [int]$WaitSeconds = 120
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  $pythonExe = $venvPython
} else {
  Write-Warning "未检测到 .venv，将使用当前 PATH 中的 python。"
  $pythonExe = "python"
}

$env:OLLAMA_MOCK_HOST = $HostAddr
$env:OLLAMA_MOCK_PORT = "$Port"
$env:OLLAMA_MOCK_BACKEND = $Backend
$env:OLLAMA_MOCK_MODEL = $Model
$env:CURSOR_WINDOW_TITLE_REGEX = $TitleRegex
$env:CURSOR_WINDOW_WAIT_SECONDS = "$WaitSeconds"
$env:PYTHONPATH = "src"

& $pythonExe -m cursor_adapter.server.ollama_mock
