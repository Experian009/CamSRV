@echo off
cd /d "%~dp0"
if not exist ".env.windows" copy /y ".env.windows.example" ".env.windows" >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 V380_GUI.py
) else (
  python V380_GUI.py
)
if errorlevel 1 pause
