# Developer build only. Not a distributable installer yet (Python runtime required).
param([ValidateSet('Debug', 'Release')][string] $Configuration = 'Debug')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw 'Install .NET 10 SDK and Visual Studio C++/WinUI build workloads first.'
}
& dotnet restore 'src\WindowsAIPhotos\WindowsAIPhotos.csproj' -p:Platform=x64
if ($LASTEXITCODE -ne 0) { throw 'dotnet restore failed' }
& dotnet build 'src\WindowsAIPhotos\WindowsAIPhotos.csproj' -c $Configuration -p:Platform=x64 --no-restore
if ($LASTEXITCODE -ne 0) { throw 'dotnet build failed' }
Write-Host 'WinUI app compiled. The Python AI worker still requires setup-dev.ps1.'
