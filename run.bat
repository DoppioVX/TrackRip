@echo off
cd /d "%~dp0"
title DoppioVault - Music Downloader

echo ========================================
echo   DoppioVault - Music Downloader
echo ========================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH
    echo Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import sys; print(f'Python {sys.version}')"
echo.

if not exist "venv" (
    echo [*] First launch - creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo [*] Installing dependencies...
    pip install --quiet flask yt-dlp
    echo [OK] Dependencies installed.
    echo.
) else (
    call venv\Scripts\activate.bat
)

:: Check ffmpeg
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "ffmpeg\ffmpeg.exe" (
        echo [!] ffmpeg not found - downloading...
        python -c "import urllib.request,zipfile,os; print('Downloading ffmpeg...'); urllib.request.urlretrieve('https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip','ffmpeg.zip'); z=zipfile.ZipFile('ffmpeg.zip'); [z.extract(f,'ffmpeg_tmp') for f in z.namelist() if 'bin/ffmpeg.exe' in f or 'bin/ffprobe.exe' in f]; z.close(); os.makedirs('ffmpeg',exist_ok=True); import glob; [os.rename(f,'ffmpeg/'+os.path.basename(f)) for f in glob.glob('ffmpeg_tmp/*/bin/*.exe')]; import shutil; shutil.rmtree('ffmpeg_tmp',True); os.remove('ffmpeg.zip'); print('OK')"
        echo [OK] ffmpeg downloaded.
    )
    set "PATH=%~dp0ffmpeg;%PATH%"
)

:: Start slskd if found and not running
tasklist /FI "IMAGENAME eq slskd.exe" 2>nul | find "slskd.exe" >nul
if %errorlevel% neq 0 (
    if exist "slskd\slskd.exe" (
        echo [*] Starting slskd...
        start "" /B /MIN slskd\slskd.exe
        timeout /t 3 /nobreak >nul
        echo [OK] slskd started on http://localhost:5030
    )
)

echo.
echo [*] Server: http://localhost:8844
echo [*] Press Ctrl+C to stop
echo.
python -u server.py

echo.
echo Server stopped.
taskkill /F /IM slskd.exe >nul 2>&1
pause
