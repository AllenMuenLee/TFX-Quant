@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\install-all.ps1" -VendorStep -BundleRoot "%~dp0.." -LogFile "%~1"
exit /b %ERRORLEVEL%
