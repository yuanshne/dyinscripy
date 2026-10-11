@echo off
chcp 65001 >nul
title 抖音工具箱
cd /d "%~dp0"

rem ============ 探测 Python（.venv / conda / py 启动器 / PATH）============
call "%~dp0_find_python.bat"
if not defined PY goto :nopy

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

:nopy
echo  [!] 未找到可用的 Python，请安装 Python 或 Anaconda/Miniconda 并加入 PATH
pause
exit /b 1

:pipfail
echo  [!] 依赖安装失败，请检查网络后重新双击本文件
pause
exit /b 1
