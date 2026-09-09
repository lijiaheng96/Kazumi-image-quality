#requires -Version 7.0
param([string]$PythonPath = '')

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $PSScriptRoot

if (-not $PythonPath) {
    $PythonPath = Join-Path $PSScriptRoot '.runtime\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw '请指定 Python 3.12 的完整路径：pwsh -File setup.ps1 -PythonPath C:\Python312\python.exe'
}
& $PythonPath -X utf8 -c "import sys; assert sys.version_info[:2] == (3,12), '需要 Python 3.12'"
if ($LASTEXITCODE -ne 0) { throw 'Python 版本检查失败' }
& $PythonPath -X utf8 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '创建本地环境失败' }
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $taskPython -X utf8 -m pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败，请检查网络后重试' }
& $taskPython -X utf8 -c "from helper.quality import MODEL; MODEL.load(); print('本地画质模型已准备好')"
if ($LASTEXITCODE -ne 0) { throw '模型准备失败，请检查网络后重试' }
Write-Host '安装完成。双击 start.cmd 或“启动画质助手.vbs”即可使用。'
