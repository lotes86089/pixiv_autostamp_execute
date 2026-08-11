@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=py"
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onefile --windowed --name PixivAutoStamp --icon pixiv_autostamp_icon.ico --add-data "pixiv_autostamp_icon.png;." --collect-all undetected_chromedriver --collect-all pystray pixiv_autostamp_gui.py
if errorlevel 1 (
  echo.
  echo Build failed. Install dependencies with:
  echo py -3.14 -m pip install -r requirements.txt
  pause
  exit /b 1
)
echo.
echo Done: dist\PixivAutoStamp.exe
pause
