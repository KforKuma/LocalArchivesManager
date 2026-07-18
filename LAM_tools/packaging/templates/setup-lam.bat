@echo off
setlocal
if "%~1"=="" (
  echo Usage: setup-lam.bat ^<new-library-directory^>
  exit /b 10
)
"%~dp0lam.exe" --root "%~1" init --apply
exit /b %ERRORLEVEL%
