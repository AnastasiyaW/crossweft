@echo off
rem the redirect comes first, so a one-digit value is plain text
call step_one.cmd
set STEP_RC=%ERRORLEVEL%
>>"%~dp0status.txt" echo step_one=%STEP_RC%
call step_two.cmd >"%~dp0step_two.log" 2>&1
