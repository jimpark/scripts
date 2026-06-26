@echo off
REM Wrapper so 'delete-branch <args>' runs the script via uv from any directory.
REM %~dp0 is this file's own folder; %* forwards all arguments to the script.
uv run "%~dp0delete-branch.py" %*
