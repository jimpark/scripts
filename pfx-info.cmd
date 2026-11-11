@echo off
REM Wrapper so 'pfx-info <args>' runs the script via uv from any directory.
REM %~dp0 is this file's own folder; %* forwards all arguments to the script.
uv run "%~dp0pfx-info.py" %*
