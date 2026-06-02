@echo off
REM Wrapper so 'configure-vscode-bedrock <args>' runs the script via uv from any directory.
REM %~dp0 is this file's own folder; %* forwards all arguments to the script.
uv run "%~dp0configure-vscode-bedrock.py" %*
