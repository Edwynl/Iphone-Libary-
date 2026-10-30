# Renames only the existing user-owned GitHub repository; never creates another.
# Needs gh CLI authenticated with repository administration permission.
param(
    [string] $Original = 'Edwynl/Iphone-Libary-',
    [string] $NewName = 'Windows-AI-Photos'
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI is required: https://cli.github.com/'
}
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'Run gh auth login first.' }
& gh repo rename $NewName --repo $Original --yes
if ($LASTEXITCODE -ne 0) { throw 'GitHub repository rename failed.' }
Write-Host "Repository renamed to https://github.com/Edwynl/$NewName"
