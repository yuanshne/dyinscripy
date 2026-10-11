@echo off
chcp 65001 >nul
title 作品爬取（命令行）
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
%PY% -c "import sys" >nul 2>nul || goto :nopy
%PY% -c "import playwright, requests" >nul 2>nul
if errorlevel 1 goto :install
goto :run

:install
echo  首次运行，正在自动安装依赖（需要联网）...
%PY% -m pip install -q playwright requests || goto :pipfail
echo  正在下载浏览器内核（仅此一次）...
%PY% -m playwright install chromium || goto :pipfail

:run
if not exist "cookies.txt" (
    echo  [!] 还没有 cookies.txt，请先双击「打开爬虫界面.bat」点「重新登录」扫码一次
    pause
    exit /b 1
)

:input
echo.
set "ACCOUNT="
set /p ACCOUNT=请输入对方抖音号或主页链接（直接回车=退出）:
if "%ACCOUNT%"=="" exit /b 0

echo.
"%PY%" douyin_spider.py --account "%ACCOUNT%" --cookie-file cookies.txt

echo.
echo ================================================
echo   已下载过的文件不会重复下载，可随时中断重跑
echo ================================================
pause
goto input

:nopy
echo  [!] 未找到可用的 Python，请先安装 Python 并勾选 Add to PATH
pause
exit /b 1

:pipfail
echo  [!] 依赖安装失败，请检查网络后重新双击本文件
pause
exit /b 1
