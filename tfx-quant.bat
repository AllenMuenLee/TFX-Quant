@echo off
rem ===========================================================================
rem  tfx-quant launcher
rem
rem  Checks the two machine prerequisites (Microsoft VC++ 2015-2022 x86 runtime
rem  and the Yuanta trade/quote OCX), tells you how to get anything that is
rem  missing, then starts the desktop app.
rem
rem  Run from the project folder (this .bat sits next to pyproject.toml).
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "MISSING=0"
set "VCREDIST_URL=https://aka.ms/vs/17/release/vc_redist.x86.exe"

echo Checking prerequisites...
echo.

rem --- Microsoft Visual C++ 2015-2022 x86 redistributable --------------------
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86" /v Installed >nul 2>&1
if errorlevel 1 reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86" /v Installed >nul 2>&1
if errorlevel 1 (
  set "MISSING=1"
  echo [MISSING] Microsoft Visual C++ 2015-2022 Redistributable ^(x86^)
  echo           Download and install:
  echo             %VCREDIST_URL%
  echo.
) else (
  echo [ ok    ] Microsoft Visual C++ x86 runtime
)

rem --- Yuanta trade API OCX -------------------------------------------------
reg query "HKCR\Yuanta.YuantaOrdCtrl.1\CLSID" >nul 2>&1
if errorlevel 1 (
  set "MISSING=1"
  echo [MISSING] Yuanta trade API  ^(ProgID Yuanta.YuantaOrdCtrl.1 not registered^)
  echo           Copy the Yuanta trade API folder to  C:\Yuanta\API
  echo           then run  C:\Yuanta\API\install_YTFutOrdAP.bat  as Administrator.
  echo.
) else (
  echo [ ok    ] Yuanta trade API
)

rem --- Yuanta quote API OCX -----------------------------------------------
reg query "HKCR\YUANTAQUOTE.YuantaQuoteCtrl.1\CLSID" >nul 2>&1
if errorlevel 1 (
  set "MISSING=1"
  echo [MISSING] Yuanta quote API  ^(ProgID YUANTAQUOTE.YuantaQuoteCtrl.1 not registered^)
  echo           Copy the Yuanta quote API folder to  C:\Yuanta\QAPI
  echo           then run  C:\Yuanta\QAPI\install_ytocx.bat  as Administrator.
  echo.
) else (
  echo [ ok    ] Yuanta quote API
)

rem --- pick a 32-bit Python 3.11 -----------------------------------------
set "PY="
set "PYQUOTE=1"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>&1 && ( set "PY=py -3.11-32" & set "PYQUOTE=0" )
)
if not defined PY if exist "%~dp0.venv64\Scripts\python.exe" set "PY=%~dp0.venv64\Scripts\python.exe"
if not defined PY (
  echo [MISSING] 32-bit Python 3.11
  echo           Create the venv:  py -3.11-32 -m venv .venv ^&^& .venv\Scripts\pip install -e .
  echo.
  pause
  exit /b 1
)

echo.
if "%MISSING%"=="1" (
  echo One or more prerequisites are missing ^(see above^).
  echo The app can still start in the TEST / simulation environment, but real
  echo trading and live market data need the items marked [MISSING].
  echo.
  pause
)

echo Starting tfx-quant...
echo   ^(close this window to stop the app^)
echo.
if "%PYQUOTE%"=="1" ( "%PY%" -m tfx_quant.desktop ) else ( %PY% -m tfx_quant.desktop )
set "RC=%errorlevel%"
echo.
echo tfx-quant exited ^(code %RC%^).
pause
endlocal
exit /b %RC%
