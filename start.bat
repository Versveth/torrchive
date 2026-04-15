@echo off
:: Torrchive — Windows launcher
:: https://github.com/Versveth/torrchive

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
set PYTHON=

:: Find Python 3.10+
for %%P in (python python3) do (
    where %%P >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%V in ('%%P -c "import sys; print(sys.version_info >= (3,10))" 2^>nul') do (
            if "%%V"=="True" (
                set PYTHON=%%P
                goto :found_python
            )
        )
    )
)

:: Try Python Launcher
where py >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%V in ('py -3 -c "import sys; print(sys.version_info >= (3,10))" 2^>nul') do (
        if "%%V"=="True" (
            set PYTHON=py -3
            goto :found_python
        )
    )
)

echo.
echo Python 3.10 or higher is required but was not found.
echo.
echo Download and install Python from:
echo   https://www.python.org/downloads/
echo.
echo IMPORTANT: Check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:found_python
for /f "tokens=*" %%V in ('%PYTHON% --version 2^>^&1') do echo Python found: %%V

:: Check/install dependencies
echo Checking dependencies...
%PYTHON% -c "import yaml, requests, rich" >nul 2>&1
if errorlevel 1 (
    echo Installing missing dependencies...
    %PYTHON% -m pip install pyyaml requests rich --quiet
    if errorlevel 1 (
        echo.
        echo Failed to install dependencies.
        echo Try running: pip install pyyaml requests rich
        echo.
        pause
        exit /b 1
    )
)

:: Check ffmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo ffmpeg is required but was not found.
    echo.
    echo Download ffmpeg from: https://www.gyan.dev/ffmpeg/builds/
    echo Extract and add the bin/ folder to your PATH.
    echo.
    echo Or install via winget:
    echo   winget install Gyan.FFmpeg
    echo.
    pause
    exit /b 1
)

:: Launch wizard if no config, otherwise launch normally
if not exist "%SCRIPT_DIR%config.yaml" (
    %PYTHON% "%SCRIPT_DIR%torrchive.py" setup --config "%SCRIPT_DIR%config.yaml"
) else (
    %PYTHON% "%SCRIPT_DIR%torrchive.py" %* --config "%SCRIPT_DIR%config.yaml"
)

pause
