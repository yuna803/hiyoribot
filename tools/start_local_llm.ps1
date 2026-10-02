param([string]$LocalHome = 'E:\unser\q\hiyori_llm')

$ErrorActionPreference = 'Stop'
$executable = Join-Path $LocalHome 'ollama\ollama.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw '未找到本机 Ollama 运行环境' }

try {
    $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3
    Write-Output '本机 Ollama 已运行。'
    exit 0
} catch { }

# 只影响本次启动的子进程；不修改系统环境变量。
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = Join-Path $LocalHome 'models'
$env:OLLAMA_CONTEXT_LENGTH = '8192'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
$env:OLLAMA_NO_CLOUD = '1'
$logs = Join-Path $LocalHome 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$process = Start-Process -FilePath $executable -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logs 'ollama-stdout.log') `
    -RedirectStandardError (Join-Path $logs 'ollama-stderr.log')
Write-Output "本机模型服务已启动，PID $($process.Id)，地址 http://127.0.0.1:11434。"
