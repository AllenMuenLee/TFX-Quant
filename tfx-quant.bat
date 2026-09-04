@echo off
rem ===========================================================================
rem  tfx-quant launcher
rem
rem  - checks the machine prerequisites (Microsoft VC++ 2015-2022 x86 runtime,
rem    Yuanta trade/quote OCX) and tells you how to get anything missing;
rem  - on first run, creates .venv with 32-bit Python 3.11 and installs the app
rem    (needs an internet connection that first time);
rem  - starts the desktop app.
rem
rem  Just double-click this file. It lives next to pyproject.toml.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "MISSING=0"
set "VENV=%~dp0.venv"
set "VENVPY=%VENV%\Scripts\python.exe"

echo Checking prerequisites...
echo.

rem --- Microsoft Visual C++ 2015-2022 x86 redistributable --------------------
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86" /v Installed >nul 2>&1
if errorlevel 1 reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86" /v Installed >nul 2>&1
if errorlevel 1 (
  set "MISSING=1"
  echo [MISSING] Microsoft Visual C++ 2015-2022 Redistributable ^(x86^)
  echo           Download and install:
  echo             https://aka.ms/vs/17/release/vc_redist.x86.exe
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

rem --- Python environment (32-bit only: the quote OCX is x86) --------------
if exist "%VENVPY%" goto have_venv

echo [ setup ] first run - preparing the Python environment
py -3.11-32 --version >nul 2>&1
if errorlevel 1 goto no_python

echo           creating .venv with 32-bit Python 3.11...
py -3.11-32 -m venv "%VENV%"
if errorlevel 1 goto venv_fail
if not exist "%VENVPY%" goto venv_fail

echo           installing tfx-quant and its dependencies ^(needs internet, ~1-2 min^)...
"%VENVPY%" -m pip install --disable-pip-version-check --upgrade pip
"%VENVPY%" -m pip install --disable-pip-version-check -e .
if errorlevel 1 goto install_fail

:have_venv
"%VENVPY%" -c "import tfx_quant.desktop" >nul 2>&1
if errorlevel 1 goto install_broken

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
"%VENVPY%" -m tfx_quant.desktop
set "RC=%errorlevel%"
echo.
echo tfx-quant exited ^(code %RC%^).
pause
endlocal & exit /b %RC%

rem ===========================================================================
:no_python
echo.
echo [MISSING] 32-bit Python 3.11 ^(the "py -3.11-32" launcher was not found^)
echo.
echo   Install 32-bit ^(x86^) Python 3.11, then double-click this file again:
echo     https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe
echo     - or -   winget install --id Python.Python.3.11 --architecture x86
echo   ^(during the python.org installer, tick "Add python.exe to PATH"^)
echo.
pause
endlocal & exit /b 1

:venv_fail
echo.
echo [ERROR] Could not create the .venv virtual environment.
echo         Delete the ".venv" folder if it exists and try again.
echo.
pause
endlocal & exit /b 1

:install_fail
echo.
echo [ERROR] "pip install" failed (see the messages above - usually no internet
echo         connection). Connect to the internet, delete the ".venv" folder,
echo         and run this file again.
echo.
pause
endlocal & exit /b 1

:install_broken
echo.
echo [ERROR] The Python environment exists but tfx-quant will not import.
echo         Delete the ".venv" folder and run this file again to rebuild it.
echo.
pause
endlocal & exit /b 1
