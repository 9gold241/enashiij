@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title ENAS HAJI Booking System

echo ================================================
echo   ENAS HAJI - Booking System
 echo ================================================
echo.

where py >nul 2>&1
if not errorlevel 1 (
    set "PYCMD=py"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found. Install Python 3.10+ and enable PATH.
        goto failure
    )
    set "PYCMD=python"
)

if not exist "requirements.txt" (
    echo [ERROR] requirements.txt is missing. Keep this file inside ENAS_HAJI_Booking_System.
    goto failure
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    %PYCMD% -m venv .venv
    if errorlevel 1 goto failure
)

echo [2/3] Checking required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failure

if not exist ".env" (
    echo [3/3] Initial setup. Create administrator login.
    ".venv\Scripts\python.exe" setup.py
    if errorlevel 1 goto failure
)

echo.
echo ================================================
echo Starting server... Keep this window OPEN.
echo WAIT for: Uvicorn running on http://127.0.0.1:8000
echo THEN open the website in your browser:
echo.
echo       http://127.0.0.1:8000/
echo       http://127.0.0.1:8000/admin
 echo.
echo Note: The browser no longer opens BEFORE the server is ready.
echo ================================================
echo.
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto failure

echo.
echo Server was stopped.
pause
exit /b 0

:failure
echo.
echo [FAILED] Look at the first error above and send a photo of this window.
echo If port 8000 is already in use, close any other server window first.
pause
exit /b 1
