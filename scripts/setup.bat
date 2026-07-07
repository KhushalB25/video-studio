@echo off
setlocal
cd /d "%~dp0.."
echo === Video Studio setup ===

if not exist ".venv" (
  echo Creating virtual environment...
  python -m venv .venv
)

echo Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -q -U pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" -m pip install -q whisperx

echo Installing Remotion dependencies...
pushd remotion
call npm install
popd

echo Creating desktop shortcut...
cscript //nologo scripts\install_desktop_shortcut.vbs

echo.
echo Setup complete. Double-click "Video Studio" on your Desktop to launch.
pause
