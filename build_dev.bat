@echo off
cd /d "%~dp0"
echo Close Shindo Screener Dev before building.
pyinstaller --noconfirm --onefile --windowed --name "ShindoScreenerDev" --icon "icon.ico" --add-data "screener_app.html;." --add-data "cities_jp.json;." --add-data "stations_jp.json;." --add-data "places_jp.json;." --add-data "municipalities_jp.json;." --add-data "icon.ico;." --add-data "alert_quiet.wav;." --add-data "alert_normal.wav;." --add-data "alert_loud.wav;." --add-data "alert_soft.wav;." --add-data "alert_beta.wav;." --add-data "voices;voices" --add-data "dev_build.flag;." app.py
echo.
echo DEV BUILD (developer features available). Do not publish this exe.
echo Done. The new exe is dist\ShindoScreenerDev.exe
pause
