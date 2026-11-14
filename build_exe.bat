@echo off
cd /d "%~dp0"
echo Close Shindo Screener before building.
pyinstaller --noconfirm --onefile --windowed --name "ShindoScreener" --icon "icon.ico" --add-data "screener_app.html;." --add-data "cities_jp.json;." --add-data "stations_jp.json;." --add-data "places_jp.json;." --add-data "municipalities_jp.json;." --add-data "icon.ico;." --add-data "alert_quiet.wav;." --add-data "alert_normal.wav;." --add-data "alert_loud.wav;." --add-data "alert_soft.wav;." --add-data "alert_beta.wav;." --add-data "voices;voices" app.py
if errorlevel 1 (
  echo.
  echo BUILD FAILED. No new exe was made.
  echo If the error says "Access is denied", Shindo Screener is still running: close it and run this again.
  pause
  exit /b 1
)
echo.
echo Done. The new exe is dist\ShindoScreener.exe
pause
