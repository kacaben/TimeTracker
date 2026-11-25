@echo off
cd /d "%~dp0"
rem Opens the browser itself once the server is listening.
python server.py --open