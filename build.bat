@echo off
echo Building Voice Changer...

REM Install runtime deps if needed
pip install -r requirements.txt

REM Install PyInstaller if needed
pip install -r requirements-dev.txt

REM Clean previous build
if exist dist\VoiceChanger rmdir /s /q dist\VoiceChanger
if exist build rmdir /s /q build

REM Build (use python -m to avoid PATH issues with Scripts directory)
python -m PyInstaller voice_changer.spec

if errorlevel 1 (
    echo BUILD FAILED
    pause
    exit /b 1
)

REM Wait for PyInstaller to release file handles before zipping
timeout /t 3 /nobreak > nul

REM Zip the output folder
echo Zipping...
powershell -Command "Compress-Archive -Path dist\VoiceChanger -DestinationPath dist\VoiceChanger.zip -Force"

echo.
echo Done! Share dist\VoiceChanger.zip with your friends.
echo Friends need to install VB-Audio Virtual Cable separately:
echo   https://vb-audio.com/Cable/
pause
