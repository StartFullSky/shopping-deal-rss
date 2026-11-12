@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:8765 in your browser.
python -X utf8 scripts\admin_server.py --open-browser
pause
