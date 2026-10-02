@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0SoftwareFetch.exe" (
    "%~dp0SoftwareFetch.exe" %*
) else if exist "%~dp0dist\SoftwareFetch.exe" (
    "%~dp0dist\SoftwareFetch.exe" %*
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 "%~dp0SoftwareFetch.py" %*
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 (
            python "%~dp0SoftwareFetch.py" %*
        ) else (
            echo No EXE or Python found. Build/download the Windows EXE from GitHub Actions.
            pause
            exit /b 2
        )
    )
)
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" echo Download finished with errors. Exit code: %EXITCODE%
pause
exit /b %EXITCODE%
