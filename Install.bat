@echo off
setlocal
cd /d "%~dp0"
echo.
echo  ScreenPilot - instalare / install
echo  Fara drepturi de administrator. Totul se instaleaza doar pentru utilizatorul curent.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Autostart -Ffmpeg -DesktopShortcut
if errorlevel 1 goto failed
echo.
echo  ============================================================
echo   Gata! ScreenPilot este instalat si pornit.
echo   - Versiunea instalata apare mai sus ("Installed: ScreenPilot x.y.z")
echo     si in aplicatie: tooltip-ul iconitei din tray si Setari - Despre.
echo   - Iconita e in tray, langa ceas (sageata ^^ daca nu o vezi).
echo   - Porneste singur odata cu Windows. Start / Desktop: ScreenPilot.
echo   - Dezinstalare: Uninstall.bat
echo  ============================================================
goto end
:failed
echo.
echo  Instalarea NU a reusit. Mesajul de eroare este mai sus.
:end
echo.
pause
