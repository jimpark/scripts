@echo off
REM Wrapper so 'latin-runs <args>' runs the script via uv from any directory.
REM %~dp0 is this file's own folder; %* forwards all arguments to the script.
uv run "%~dp0latin-runs.py" %*
