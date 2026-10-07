@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem ASCII ONLY: cmd parses .bat as GBK.
rem Runs all test suites.

set "PYEXE=py"
where py >nul 2>nul
if errorlevel 1 set "PYEXE=python"

set PASS=0
set FAIL=0

for %%T in (test_pse test_acl test_config test_characters test_settings test_voice test_hands test_browser_protocol test_model3d test_circadian test_shop) do (
    echo.
    echo === %%T ===
    %PYEXE% tests\%%T.py
    if errorlevel 1 (set /a FAIL+=1) else (set /a PASS+=1)
)

echo.
echo ==========================================
echo suites passed: %PASS%
echo suites failed: %FAIL%
echo ==========================================
pause
endlocal
