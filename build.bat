@echo off
setlocal
cd /d "%~dp0"

echo ==== ymir build ====

if not exist "main.py" (
    echo main.py not found next to build.bat
    pause
    exit /b 1
)
if not exist "image\" (
    echo The "image" folder was not found next to build.bat
    pause
    exit /b 1
)

rem ---------------- options ----------------
rem 1 = compile PyInstaller's bootloader from source (fewer antivirus hits).
rem     Needs "Visual Studio Build Tools" with the C++ workload installed.
rem     If it fails, the script falls back to the normal PyInstaller.
set COMPILE_BOOTLOADER=1

rem Admin prompt. Only keep this if the macro really needs it to send input
rem to the game. Removing it lowers detections. To remove: set ADMIN_FLAG=
set ADMIN_FLAG=--uac-admin
rem -----------------------------------------

rem ---- use the project's .venv (create it if missing) ----
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)
set PY=".venv\Scripts\python.exe"

echo Installing / updating requirements...
%PY% -m pip install --upgrade pip
%PY% -m pip install mss opencv-python numpy pydirectinput keyboard
if errorlevel 1 (
    echo Could not install the requirements.
    pause
    exit /b 1
)

if "%COMPILE_BOOTLOADER%"=="1" (
    echo Installing PyInstaller and compiling its bootloader from source...
    %PY% -m pip install --upgrade --force-reinstall --no-binary pyinstaller pyinstaller
    if errorlevel 1 (
        echo Source build failed, falling back to the normal PyInstaller.
        %PY% -m pip install --upgrade pyinstaller
    )
) else (
    %PY% -m pip install --upgrade pyinstaller
)
if errorlevel 1 (
    echo Could not install PyInstaller.
    pause
    exit /b 1
)

set VERSION_FLAG=
if exist "version_info.txt" set VERSION_FLAG=--version-file "version_info.txt"

echo Building...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --noupx %ADMIN_FLAG% ^
    %VERSION_FLAG% ^
    --name ymir ^
    --icon "image\ymir.ico" ^
    --add-data "image;image" ^
    --add-data "change.md;." ^
    --add-data "guides;guides" ^
    main.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Writing SHA-256 hash...
certutil -hashfile "dist\ymir.exe" SHA256 | findstr /v "hash CertUtil" > "dist\ymir.sha256.txt"
type "dist\ymir.sha256.txt"

echo.
echo Done! Your exe is in the "dist" folder: dist\ymir.exe
echo Put the hash above in your GitHub release notes, and upload the exe to
echo VirusTotal / submit it to Microsoft as a false positive if it gets flagged.
echo (The "image" folder is bundled inside it. Stats are saved in Documents\Ymir Macro.)
pause
