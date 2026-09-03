@echo off
setlocal EnableExtensions
set "SRC=%~dp0"
set "LOG=%~1"
if "%LOG%"=="" set "LOG=%TEMP%\tfx-quant-vendor-install.log"
echo [%DATE% %TIME%] vendor install started>>"%LOG%"

if exist "%SRC%vc_redist.x86.exe" (
  echo [%DATE% %TIME%] vc_redist.x86.exe /install /quiet /norestart>>"%LOG%"
  "%SRC%vc_redist.x86.exe" /install /quiet /norestart>>"%LOG%" 2>&1
  echo [%DATE% %TIME%] vc_redist exit %ERRORLEVEL%>>"%LOG%"
)

if exist "%SRC%API\YuantaOrd.ocx" (
  if not exist "C:\Yuanta\API" mkdir "C:\Yuanta\API"
  xcopy /e /i /y /q "%SRC%API\*" "C:\Yuanta\API\">>"%LOG%" 2>&1
  regsvr32 /s "C:\Yuanta\API\YuantaOrd.ocx"
  echo [%DATE% %TIME%] regsvr32 YuantaOrd.ocx exit %ERRORLEVEL%>>"%LOG%"
)

if exist "%SRC%QAPI\YuantaQuote_v2.1.2.9.ocx" (
  if not exist "C:\Yuanta\QAPI" mkdir "C:\Yuanta\QAPI"
  xcopy /e /i /y /q "%SRC%QAPI\*" "C:\Yuanta\QAPI\">>"%LOG%" 2>&1
  regsvr32 /s "C:\Yuanta\QAPI\YuantaQuote_v2.1.2.9.ocx"
  echo [%DATE% %TIME%] regsvr32 YuantaQuote exit %ERRORLEVEL%>>"%LOG%"
)

echo [%DATE% %TIME%] vendor install finished>>"%LOG%"
endlocal
exit /b 0
