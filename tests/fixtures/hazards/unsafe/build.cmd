@echo off
call prepare.cmd
set RC=%ERRORLEVEL%
if not "%RC%"=="0" exit /b %RC%
cmake -S . -B out
cmake --build out --config Release
