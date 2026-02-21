@echo off
rem Arranger launcher for Windows.
rem Starts the main application from the same directory as this script.

set "HERE=%~dp0"
"%HERE%arranger.exe" %*
