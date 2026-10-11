@echo off
chcp 65001 >nul
title 构建抖音工具箱便携版
cd /d "%~dp0"

rem 需要：本机已装 Python + playwright + imageio-ffmpeg + pyinstaller
python -m PyInstaller douyin_toolbox.spec --noconfirm || goto :err
copy /y "%~dp0包内使用说明.txt" "%~dp0dist\抖音工具箱\使用说明.txt" >nul

rem 构建会把 dist 清空，Chromium 内核必须在构建之后放进包里
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0dist\抖音工具箱\runtime\ms-playwright"
set "PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright"
python -m playwright install chromium || goto :err

echo.
echo 构建完成: dist\抖音工具箱
pause
exit /b 0

:err
echo [!] 构建失败，请看上方报错
pause
exit /b 1
