@echo off
setlocal
cd /d "%~dp0"

echo.
echo Building Music Cat...
echo.

python --version

echo.
echo Installing PyInstaller...
python -m pip install --upgrade pyinstaller

if errorlevel 1 (
    echo.
    echo PyInstaller install failed.
    pause
    exit /b 1
)

echo.
echo Building app...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --icon "music_cat.ico" ^
  --add-data "music_cat.ico;." ^
  --name "Music Cat" ^
  "music_cat.py"

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo ==============================================
echo DONE!
echo Your app is here:
echo %~dp0dist\Music Cat.exe
echo ==============================================
echo.
pause
