@echo off
cd /d "%~dp0"
python -c "import PySide6, mss, numpy" 2>nul || python -m pip install -r requirements.txt
start "" pythonw crt_filter.py
