@echo off
setlocal

echo.
echo  jj-dlp ^| FFmpeg Installer
echo  ----------------------------------------
echo.

:: Check if winget is available
where winget >nul 2>&1
if errorlevel 1 (
    echo  ERROR: winget is not available on this system.
    echo.
    echo  You can install ffmpeg manually from:
    echo    https://ffmpeg.org/download.html
    echo.
    echo  Or enable winget via the Microsoft Store:
    echo    https://aka.ms/getwinget
    echo.
    pause
    exit /b 1
)

:: Check if ffmpeg is already installed
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo  ffmpeg is already installed and available on your PATH.
    echo.
    ffmpeg -version 2>&1 | findstr /i "ffmpeg version"
    echo.
    pause
    exit /b 0
)

echo  Installing ffmpeg via winget...
echo.

winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements

if errorlevel 1 (
    echo.
    echo  ERROR: winget installation failed.
    echo.
    echo  Try running this script as Administrator, or install ffmpeg manually:
    echo    https://ffmpeg.org/download.html
    echo.
    pause
    exit /b 1
)

echo.
echo  ----------------------------------------
echo  ffmpeg installed successfully!
echo.
echo  NOTE: You may need to restart your terminal for ffmpeg to be
echo  recognized on your PATH.
echo.
echo  In your jj-dlp config, set:
echo    FFMPEG_PATH = ffmpeg
echo  to use the system-installed version.
echo  ----------------------------------------
echo.
pause
endlocal
