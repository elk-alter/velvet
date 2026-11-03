@echo off
cd /d "%~dp0"
python service_control.py start --open %*
if errorlevel 1 pause
