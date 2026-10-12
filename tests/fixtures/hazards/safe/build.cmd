@echo off
call prepare.cmd
set PREPARE_RC=%ERRORLEVEL%
if not "%PREPARE_RC%"=="0" exit /b %PREPARE_RC%
set INCLUDE=%INCLUDE%;%~dp0third_party\include
set CC=clang-cl
cmake -S . -B out
cmake --build out --config Release
