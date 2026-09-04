@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Project virtual environment not found.
    echo Run the installation commands in README.md first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo Configuration file .env not found.
    echo Copy .env.example to .env and fill in the required values.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m ocean_helper_bot
set "exit_code=%ERRORLEVEL%"

if not "%exit_code%"=="0" (
    echo.
    echo OceanHelperBot exited with code %exit_code%.
    pause
)

exit /b %exit_code%
