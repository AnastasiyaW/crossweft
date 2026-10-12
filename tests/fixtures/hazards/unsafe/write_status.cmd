@echo off
rem a step runner that records each exit code for a reader in another language
call step_one.cmd
set STEP_RC=%ERRORLEVEL%
echo step_one=%STEP_RC%>>"%~dp0status.txt"
