@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem ASCII ONLY: cmd parses .bat as GBK.
set "PYEXE=py"
where py >nul 2>nul
if errorlevel 1 set "PYEXE=python"

%PYEXE% -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [dependencies missing] "%PYEXE%" cannot import PyQt6.
    echo Please run: pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

%PYEXE% main.py
set EXITCODE=%errorlevel%

if not "%EXITCODE%"=="0" (
    echo.
    echo [start failed] exit code %EXITCODE%
    echo Screenshot the messages above and send them for help.
    pause
)
endlocal
