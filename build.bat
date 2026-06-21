@echo off
echo ========================================
echo StreamVault Build Script
echo ========================================
echo.

REM Check if PyInstaller is installed
python -m PyInstaller --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller not found. Installing...
    pip install pyinstaller
)

REM Clean previous builds
echo [1/3] Cleaning previous builds...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
echo [OK] Clean complete
echo.

REM Build executables with PyInstaller
echo [2/3] Building executables with PyInstaller...
python -m PyInstaller StreamVault.spec --clean
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller build failed for StreamVault!
    pause
    exit /b 1
)

python -m PyInstaller Backend.spec --clean
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller build failed for backend!
    pause
    exit /b 1
)
echo [OK] Executable built successfully
echo.

REM Build installer with Inno Setup
echo [3/3] Building installer with Inno Setup...
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    "C:\Program Files\Inno Setup 6\ISCC.exe" installer.iss
) else if exist "C:\Program Files (x86)\Inno Setup 5\ISCC.exe" (
    "C:\Program Files (x86)\Inno Setup 5\ISCC.exe" installer.iss
) else if exist "C:\Program Files\Inno Setup 5\ISCC.exe" (
    "C:\Program Files\Inno Setup 5\ISCC.exe" installer.iss
) else (
    echo [WARNING] Inno Setup not found in standard locations.
    echo Please install Inno Setup from https://jrsoftware.org/isdl.php
    echo Or run ISCC.exe manually: ISCC.exe installer.iss
    echo.
    echo The executable has been built in dist\StreamVault.exe
    pause
    exit /b 0
)

if %errorlevel% neq 0 (
    echo [ERROR] Inno Setup build failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo Build Complete!
echo ========================================
echo Installer: Output\StreamVault-Setup.exe
echo Executable: dist\StreamVault.exe
echo.
echo You can now run the installer to install StreamVault.
echo After installation, StreamVault will be available in:
echo - Start Menu (can be pinned)
echo - Desktop shortcut
echo.
pause
