@echo off
chcp 65001 >nul
setlocal EnableExtensions
REM Windows AI Photos 一键启动（开发版）
REM   双击：直接启动；首次会自动创建 .venv 并安装引擎依赖，没有构建产物时自动编译
REM   Launch-WindowsAIPhotos.bat build ：强制重新编译后再启动
REM   图库、缩略图和 AI 模型都在 %LOCALAPPDATA%\WindowsAIPhotos

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PROJ=%ROOT%src\WindowsAIPhotos\WindowsAIPhotos.csproj"
set "EXE=%ROOT%src\WindowsAIPhotos\bin\x64\Release\net10.0-windows10.0.19041.0\WindowsAIPhotos.exe"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

REM ---------- .NET 10：优先用项目内的本地 SDK，否则用系统 dotnet ----------
set "DOTNET=dotnet"
if exist "%ROOT%docs\review\.dotnet\dotnet.exe" (
    set "DOTNET_ROOT=%ROOT%docs\review\.dotnet"
    set "DOTNET=%ROOT%docs\review\.dotnet\dotnet.exe"
)

REM ---------- Python 引擎环境 ----------
if exist "%VENV_PY%" goto :python_ready
echo [1/3] 首次运行：创建 Python 虚拟环境 .venv ...
py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv || python -m venv .venv
if not exist "%VENV_PY%" (
    echo 创建 .venv 失败，请先安装 Python 3.11+。
    goto :fail
)
echo [2/3] 安装引擎和 AI 依赖（Pillow / numpy / pi-heif / torch / transformers，较大）...
"%VENV_PY%" -m pip install --disable-pip-version-check -q --require-hashes -r engine\requirements-ai.lock.txt
if errorlevel 1 (
    echo 依赖安装失败。
    rmdir /s /q .venv
    goto :fail
)
:python_ready
set "AI_PHOTOS_PYTHON=%VENV_PY%"

REM ---------- 编译（缺少产物或传入 build 参数时） ----------
if /i "%~1"=="build" goto :build
if exist "%EXE%" goto :launch
:build
echo [3/3] 编译 WinUI 应用（Release x64）...
"%DOTNET%" build "%PROJ%" -c Release -p:Platform=x64 -nologo -v:q
if errorlevel 1 (
    echo 编译失败。需要 .NET 10 SDK 和 Windows App SDK 构建环境。
    goto :fail
)

:launch
if not exist "%EXE%" (
    echo 找不到 %EXE%
    goto :fail
)
echo 正在启动 Windows AI Photos ...
start "" "%EXE%"
exit /b 0

:fail
echo.
pause
exit /b 1
