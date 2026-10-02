@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (set "PY=py -3") else (set "PY=python")
%PY% -m pip install -r requirements-build.txt || goto :fail
%PY% -m PyInstaller --noconfirm --clean --onefile --console --name SoftwareFetch --add-data "config.json;." SoftwareFetch.py || goto :fail
copy /y config.json dist\config.json >nul || goto :fail
copy /y Run-Windows.cmd dist\Run-Windows.cmd >nul || goto :fail
echo Built dist\SoftwareFetch.exe
pause
exit /b 0
:fail
echo Build FAILED
pause
exit /b 1
