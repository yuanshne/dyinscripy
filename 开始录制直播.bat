@echo off
chcp 65001 >nul
title 直播录制
cd /d "%~dp0"

rem ============ 探测 Python（.venv / conda / py 启动器 / PATH）============
call "%~dp0_find_python.bat"
if not defined PY goto :nopy

%PY% -c "import requests" >nul 2>nul
if errorlevel 1 goto :install
goto :run

:install
echo  首次运行，正在自动安装依赖（需要联网）...
%PY% -m pip install -q requests || goto :pipfail

:run
if not exist "cookies.txt" (
    echo  [提示] 没找到 cookies.txt，未登录状态也能录制大部分直播
)

echo ================================================
echo   直播录制（原始流纯净录制：无弹幕/评论/人数）
echo   主播没开播时脚本会一直等，开播自动开始录
echo   录制过程中按 Ctrl+C 随时停止
echo ================================================
echo.
"%PY%" douyin_live_recorder.py

echo.
pause
exit /b 0

:nopy
echo  [!] 未找到可用的 Python，请安装 Python 或 Anaconda/Miniconda 并加入 PATH
pause
exit /b 1

:pipfail
echo  [!] 依赖安装失败，请检查网络后重新双击本文件
pause
exit /b 1
