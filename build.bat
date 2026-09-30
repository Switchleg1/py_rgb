@echo off
setlocal EnableDelayedExpansion
REM ---------------------------------------------------------------------
REM  py_rgb build script
REM
REM    build.bat            build both executables into dist\py_rgb
REM    build.bat clean      remove build/, dist/, *.spec and __pycache__
REM    build.bat deps       install/refresh build + runtime dependencies
REM    build.bat onedir     build as a folder instead of single files
REM    build.bat test       run the test suite, then build
REM ---------------------------------------------------------------------

cd /d "%~dp0"

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

set "DIST=dist\py_rgb"
set "ONEFLAG=--onefile"
set "MODE=onefile"
set "RUNTESTS=0"

:parse
if "%~1"=="" goto after_parse
if /I "%~1"=="clean"  goto do_clean
if /I "%~1"=="deps"   goto do_deps
if /I "%~1"=="onedir" (set "ONEFLAG=--onedir" & set "MODE=onedir")
if /I "%~1"=="test"   set "RUNTESTS=1"
shift
goto parse
:after_parse

echo ==========================================================
echo  py_rgb build  ^(%MODE%^)
echo ==========================================================

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 or newer is required.
    exit /b 1
)

%PY% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [*] PyInstaller missing - installing...
    %PY% -m pip install --quiet pyinstaller || (echo [ERROR] could not install PyInstaller & exit /b 1)
)

if "%RUNTESTS%"=="1" (
    echo [*] Running tests...
    %PY% tests\test_pyrgb.py || (echo [ERROR] tests failed & exit /b 1)
)

echo [*] Cleaning previous output...
if exist build rmdir /s /q build
if exist "%DIST%" rmdir /s /q "%DIST%"

REM --- shared PyInstaller options ------------------------------------
REM hidden imports: optional backends/sources PyInstaller cannot see
set "COMMON=%ONEFLAG% --noconfirm --clean --distpath %DIST% --workpath build --specpath build"
set "COMMON=%COMMON% --hidden-import=hid --hidden-import=openrgb --hidden-import=psutil"
set "COMMON=%COMMON% --hidden-import=soundcard --hidden-import=sounddevice --hidden-import=numpy"
set "COMMON=%COMMON% --collect-binaries=soundcard --collect-binaries=sounddevice --collect-binaries=hid"
set "COMMON=%COMMON% --exclude-module=tkinter --exclude-module=pytest --exclude-module=matplotlib"

if exist assets\pyrgb.ico set "COMMON=%COMMON% --icon=assets\pyrgb.ico"

echo [*] Building pyrgb.exe   ^(console: CLI, daemon, service^)...
%PY% -m PyInstaller %COMMON% --console --name pyrgb scripts\pyrgb_cli.py
if errorlevel 1 (echo [ERROR] console build failed & exit /b 1)

echo [*] Building pyrgbw.exe  ^(windowed: Qt6 GUI + background daemon^)...
%PY% -m PyInstaller %COMMON% --windowed --name pyrgbw scripts\pyrgb_gui.py
if errorlevel 1 (echo [ERROR] windowed build failed & exit /b 1)

REM --- ship a default config next to the executables -----------------
if not exist "%DIST%\config.toml" (
    echo [*] Writing default config.toml...
    %PY% -c "from pyrgb.config import default_config, save_config; from pathlib import Path; save_config(default_config(), Path(r'%DIST%\config.toml'))"
)
if exist README.md copy /y README.md "%DIST%\README.md" >nul

echo.
echo ==========================================================
echo  Build complete: %CD%\%DIST%
echo ==========================================================
dir /b "%DIST%"
echo.
echo  pyrgb.exe   - CLI      ^(pyrgb.exe devices ^| doctor ^| run ^| ctl ...^)
echo  pyrgbw.exe  - GUI      ^(double-click^) and daemon ^(pyrgbw.exe daemon^)
echo.
echo  Autostart is OFF by default. Enable it from the GUI
echo  ^(Startup -^> "Start py_rgb with Windows"^) or: pyrgb.exe service install
echo.
exit /b 0

REM ---------------------------------------------------------------------
:do_clean
echo [*] Cleaning...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist py_rgb.egg-info rmdir /s /q py_rgb.egg-info
del /s /q *.spec >nul 2>&1
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
echo [*] Done.
exit /b 0

REM ---------------------------------------------------------------------
:do_deps
echo [*] Installing dependencies...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
%PY% -m pip install pyinstaller
echo [*] Done.
exit /b 0
