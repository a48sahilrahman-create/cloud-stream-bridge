@echo off
title CloudStream WebDAV Bridge
cd /d "%~dp0"
echo ========================================================
echo  Starting CloudStream WebDAV Bridge on http://localhost:7860
echo ========================================================
python -m uvicorn main:app --host 0.0.0.0 --port 7860
pause
