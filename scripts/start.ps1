<#
.SYNOPSIS
使用既有 Conda 环境启动本机 API 与持久 Worker。

.DESCRIPTION
Engine MCP 只由 Worker 按需以 stdio 子进程启动；本脚本不会创建或激活新的 Python 环境。
#>

param(
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$condaEnvironment = "unofficial-davinci-mcp-win"
$condaExecutable = if ($env:CONDA_EXE -and (Test-Path $env:CONDA_EXE)) {
    $env:CONDA_EXE
} else {
    (Get-Command conda.exe -ErrorAction Stop).Source
}
$env:PYTHONPATH = "$repositoryRoot\src;$repositoryRoot\davinci-engine-mcp\src"

# 先通过 Conda 运行一次只读检查，避免错误解释器静默启动产品进程。
& $condaExecutable run --no-capture-output -n $condaEnvironment python -c "import os, sys; assert os.environ.get('CONDA_DEFAULT_ENV') == '$condaEnvironment'; assert sys.version_info[:3] == (3, 10, 20)"
if ($LASTEXITCODE -ne 0) {
    throw "既有 Conda 环境 $condaEnvironment 不可用或版本不符合合同。"
}

$worker = Start-Process -FilePath $condaExecutable -ArgumentList @(
    "run", "--no-capture-output", "-n", $condaEnvironment,
    "python", "-m", "davinci_app", "worker"
) -WorkingDirectory $repositoryRoot -WindowStyle Hidden -PassThru

try {
    & $condaExecutable run --no-capture-output -n $condaEnvironment python -m davinci_app api --host 127.0.0.1 --port $Port
}
finally {
    if (-not $worker.HasExited) {
        Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue
    }
}
