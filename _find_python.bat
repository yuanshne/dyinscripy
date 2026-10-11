@echo off
rem ============================================================
rem  Shared Python detection (called by other .bat, sets PY)
rem  Order: project .venv -> active conda env -> conda base
rem         -> py launcher -> python on PATH
rem  Dynamic detection only, no machine-specific paths.
rem  NOTE: keep this file ASCII-only (cmd parses it before
rem  the caller's chcp 65001 takes effect in some contexts).
rem ============================================================

set "PY="

rem 1) project-local venv
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if defined PY goto :eof

rem 2) currently activated conda env
if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "PY=%CONDA_PREFIX%\python.exe"
if defined PY goto :eof

rem 3) conda installed -> ask it where base env is
where conda >nul 2>nul && (
    for /f "delims=" %%i in ('conda info --base 2^>nul') do (
        if exist "%%i\python.exe" set "PY=%%i\python.exe"
    )
)
if defined PY goto :eof

rem 4) py launcher (ships with official installer)
where py >nul 2>nul && set "PY=py"
if defined PY goto :eof

rem 5) python on PATH
where python >nul 2>nul && set "PY=python"
goto :eof
