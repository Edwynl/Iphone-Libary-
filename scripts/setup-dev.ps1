# Windows PowerShell 5.1 or PowerShell 7+. Does not install model weights.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.11+ is required. Install it from python.org and retry.'
}
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
}
$python = Join-Path $root '.venv\Scripts\python.exe'
& $python -m pip install --upgrade pip
& $python -m pip install -r 'engine\requirements-test.txt'
if ($LASTEXITCODE -ne 0) { throw 'Installing core dependencies failed' }
Write-Host 'Core engine installed and ready. AI weights have NOT been downloaded.'
Write-Host 'To enable AI, run: .\.venv\Scripts\python.exe -m pip install -r engine\requirements-ai.txt'
Write-Host 'Set your interpreter before launching WinUI:'
Write-Host ('$env:AI_PHOTOS_PYTHON = "' + $python + '"')
Write-Host 'Windows project: dotnet run --project src\WindowsAIPhotos\WindowsAIPhotos.csproj -p:Platform=x64'
