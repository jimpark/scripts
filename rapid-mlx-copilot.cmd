@echo off
REM Wrapper so 'rapid-mlx-copilot <args>' runs the script via uv from any directory.
REM %~dp0 is this file's own folder; %* forwards all arguments to the script.
REM (MLX itself is Apple-silicon only; this exists so every script has both wrappers.)
uv run "%~dp0rapid-mlx-copilot.py" %*
