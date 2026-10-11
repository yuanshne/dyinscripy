@echo off
chcp 65001 >nul
title 抖音工具箱
cd /d "%~dp0"

rem ============ 找一个可用的 Python ============
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if defined PY goto :checkpy
if exist "C:\Users\A\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=C:\Users\A\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if defined PY goto :checkpy
where py >nul 2>nul && set "PY=py"
if defined PY goto :checkpy
where python >nul 2>nul && set "PY=python"
if defined PY goto :checkpy

echo  [!] 未找到 Python，请先安装 Python 并勾选 Add to PATH
pause
exit /b 1

:checkpy
%PY% -c "import sys" >nul 2>nul || goto :nopython2

rem ============ 首次运行自动装依赖 ============
%PY% -c "import playwright, requests" >nul 2>nul
if errorlevel 1 goto :install
goto :run

:install
echo  首次运行，正在自动安装依赖（需要联网）...
%PY% -m pip install -q playwright requests || goto :pipfail
echo  正在下载浏览器内核（仅此一次）...
%PY% -m playwright install chromium || goto :pipfail

:run
echo  正在启动抖音工具箱，浏览器会自动打开（关掉本窗口 = 退出）...
%PY% douyin_ui.py
goto :eof

:nopython2
echo  [!] 未找到可用的 Python，请先安装 Python 并勾选 Add to PATH
pause
exit /b 1

:pipfail
echo  [!] 依赖安装失败，请检查网络后重新双击本文件
pause
exit /b 1
