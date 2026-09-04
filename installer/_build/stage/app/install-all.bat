@echo off
rem ===========================================================================
rem  install-all.bat - one-bundle installer for tfx-quant
rem
rem  Installs the app, its runtime, and every dependency the client needs
rem  (Microsoft VC++ x86 redistributable + the Yuanta trade/quote OCX) from the
rem  bundle folder this script sits in. Pure batch - no PowerShell policy.
rem
rem  Bundle layout (produced by installer/build.py):
rem      install-all.bat
rem      runtime\  Lib\  src\  launcher.pyw  tfx-quant-desktop.cmd
rem      build-manifest.json  SHA256SUMS  third_party_licenses.txt  RELEASE-NOTES-*.md
rem      vendor\  (vc_redist.x86.exe, API\, QAPI\)   "with components" build only
rem
rem  Usage:
rem      install-all.bat                    app + dependencies (one UAC prompt)
rem      install-all.bat -DependenciesOnly  VC++ + Yuanta OCX only
rem      install-all.bat -AppOnly           app only
rem      install-all.bat -DryRun            show actions, change nothing
rem      install-all.bat -InstallDir "C:\path"
rem      install-all.bat -Uninstall [-RemoveData] [-RemoveYuanta]
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion

set "SELF=%~f0"
set "BUNDLE=%~dp0"
if "%BUNDLE:~-1%"=="\" set "BUNDLE=%BUNDLE:~0,-1%"

set "APPNAME=tfx-quant"
set "MODE=full"
set "DRYRUN=0"
set "NOSHORTCUTS=0"
set "REMOVEDATA=0"
set "REMOVEYUANTA=0"
set "INSTALLDIR=%LOCALAPPDATA%\Programs\%APPNAME%"
set "ULAD=%LOCALAPPDATA%"
set "LOGFILE="
set "UNINSTKEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\%APPNAME%"
set "TRADE_PROGID=Yuanta.YuantaOrdCtrl.1"
set "QUOTE_PROGID=YUANTAQUOTE.YuantaQuoteCtrl.1"
set "YUANTA_API=C:\Yuanta\API"
set "YUANTA_QAPI=C:\Yuanta\QAPI"
set "OCX_TRADE=YuantaOrd.ocx"
set "OCX_QUOTE=YuantaQuote_v2.1.2.9.ocx"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="-DependenciesOnly" ( set "MODE=deps"       & shift & goto parse )
if /i "%~1"=="-AppOnly"          ( set "MODE=app"        & shift & goto parse )
if /i "%~1"=="-Uninstall"        ( set "MODE=uninstall"  & shift & goto parse )
if /i "%~1"=="-VendorStep"       ( set "MODE=vendorstep" & shift & goto parse )
if /i "%~1"=="-DryRun"           ( set "DRYRUN=1"        & shift & goto parse )
if /i "%~1"=="-NoShortcuts"      ( set "NOSHORTCUTS=1"   & shift & goto parse )
if /i "%~1"=="-RemoveData"       ( set "REMOVEDATA=1"    & shift & goto parse )
if /i "%~1"=="-RemoveYuanta"     ( set "REMOVEYUANTA=1"  & shift & goto parse )
if /i "%~1"=="-InstallDir"       ( set "INSTALLDIR=%~2"  & shift & shift & goto parse )
if /i "%~1"=="-UserLocalAppData" ( set "ULAD=%~2"        & shift & shift & goto parse )
if /i "%~1"=="-Log"              ( set "LOGFILE=%~2"     & shift & shift & goto parse )
echo Unknown option: %~1
shift
goto parse
:parsed

set "DATADIR=%ULAD%\tfx_quant"

if not defined LOGFILE (
  set "DT="
  for /f "tokens=2 delims==." %%t in ('wmic os get localdatetime /value 2^>nul') do if not defined DT set "DT=%%t"
  if not defined DT set "DT=%RANDOM%%RANDOM%"
  set "LOGFILE=%DATADIR%\logs\install-all-!DT!.log"
)

set "APPVER=0.0.0"
if exist "%BUNDLE%\build-manifest.json" (
  for /f "tokens=2 delims=:," %%v in ('findstr /c:"\"app_version\"" "%BUNDLE%\build-manifest.json"') do set "APPVER=%%~v"
)
set "APPVER=%APPVER: =%"
set "APPVER=%APPVER:"=%"

if not exist "%DATADIR%\logs" if "%DRYRUN%"=="0" mkdir "%DATADIR%\logs" 2>nul
call :log run_started "mode-%MODE%"

if /i "%MODE%"=="vendorstep" goto MODE_VENDORSTEP
if /i "%MODE%"=="uninstall"  goto MODE_UNINSTALL

rem ------------------------------------------------------------- install ------
set "DEPS_OK=1"

if /i not "%MODE%"=="deps" call :install_app
if errorlevel 1 goto FAIL

if /i "%MODE%"=="app" goto DONE

call :have_vendor
if errorlevel 1 goto NO_VENDOR

call :run_vendor_elevated
if errorlevel 1 set "DEPS_OK=0"
goto DONE

:NO_VENDOR
echo.
echo [i] This bundle has no components payload [app-only build].
echo     Install the Yuanta API and VC++ x86 runtime per the installation manual.
call :log vendor_absent "-"

:DONE
call :summary
call :log run_finished "deps_ok-%DEPS_OK%"
if "%DEPS_OK%"=="0" echo.
if "%DEPS_OK%"=="0" echo [!] Dependencies not finished. Re-run:  "%SELF%" -DependenciesOnly
endlocal & exit /b 0

:MODE_VENDORSTEP
call :do_vendor
call :log run_finished "vendorstep"
endlocal & exit /b 0

rem ===========================================================================
:install_app
echo.
echo Installing %APPNAME% %APPVER%  to  %INSTALLDIR%
for %%d in (config logs backup data) do (
  if not exist "%DATADIR%\%%d" if "%DRYRUN%"=="0" mkdir "%DATADIR%\%%d" 2>nul
)
if "%DRYRUN%"=="1" goto install_app_after_copy
if not exist "%INSTALLDIR%" mkdir "%INSTALLDIR%" 2>nul
robocopy "%BUNDLE%" "%INSTALLDIR%" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
if errorlevel 8 goto install_app_copyfail
:install_app_after_copy
if "%DRYRUN%"=="1" echo   [dry-run] would copy the bundle to "%INSTALLDIR%"
call :log app_files_installed "version-%APPVER%"

if "%NOSHORTCUTS%"=="1" goto install_app_reg
set "DESKTOPDIR=%USERPROFILE%\Desktop"
for /f "tokens=2,*" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v Desktop 2^>nul ^| find "REG_"') do set "DESKTOPDIR=%%b"
call :make_shortcut "%APPDATA%\Microsoft\Windows\Start Menu\Programs\%APPNAME%.lnk"
if exist "%DESKTOPDIR%" call :make_shortcut "%DESKTOPDIR%\%APPNAME%.lnk"
call :log shortcuts_created "-"

:install_app_reg
if "%DRYRUN%"=="1" goto install_app_done
set "UNS=\"%INSTALLDIR%\install-all.bat\" -Uninstall"
reg add "%UNINSTKEY%" /v DisplayName     /d "%APPNAME% %APPVER%"  /f >nul
reg add "%UNINSTKEY%" /v DisplayVersion  /d "%APPVER%"            /f >nul
reg add "%UNINSTKEY%" /v Publisher       /d "tfx-quant"           /f >nul
reg add "%UNINSTKEY%" /v InstallLocation /d "%INSTALLDIR%"        /f >nul
reg add "%UNINSTKEY%" /v UninstallString /d "!UNS!"               /f >nul
reg add "%UNINSTKEY%" /v NoModify /t REG_DWORD /d 1 /f >nul
reg add "%UNINSTKEY%" /v NoRepair /t REG_DWORD /d 1 /f >nul
call :log addremove_entry_written "-"

:install_app_done
echo   done.
exit /b 0

:install_app_copyfail
call :log app_copy_failed "robocopy-%errorlevel%"
echo   [!] file copy failed
exit /b 1

rem ===========================================================================
:do_vendor
call :log dependencies_started "-"
call :install_vcredist
call :install_ocx "%BUNDLE%\vendor\API"  "%YUANTA_API%"  "%OCX_TRADE%" "%TRADE_PROGID%" trade
call :install_ocx "%BUNDLE%\vendor\QAPI" "%YUANTA_QAPI%" "%OCX_QUOTE%" "%QUOTE_PROGID%" quote
call :log dependencies_finished "-"
exit /b 0

:install_vcredist
set "VCR=%BUNDLE%\vendor\vc_redist.x86.exe"
if not exist "%VCR%" ( call :log vcredist_absent "-" & exit /b 0 )
echo   Microsoft VC++ x86 runtime...
if "%DRYRUN%"=="1" ( echo     [dry-run] vc_redist.x86.exe /install /quiet /norestart & exit /b 0 )
"%VCR%" /install /quiet /norestart
set "RC=%errorlevel%"
set "OK=0"
if "%RC%"=="0" set "OK=1"
if "%RC%"=="3010" set "OK=1"
if "%RC%"=="1638" set "OK=1"
call :log vcredist_installed "exit-%RC%-ok-%OK%"
echo     exit %RC%
exit /b 0

:install_ocx
rem %1 src  %2 dest  %3 ocx  %4 progid  %5 label
if not exist "%~1\%~3" ( call :log yuanta_absent "component-%~5" & exit /b 0 )
echo   Yuanta %~5 API to %~2 ...
if "%DRYRUN%"=="1" ( echo     [dry-run] copy + register %~3 & exit /b 0 )
if not exist "%~2" mkdir "%~2" 2>nul
robocopy "%~1" "%~2" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
set "REGSVR=%SystemRoot%\System32\regsvr32.exe"
if exist "%SystemRoot%\SysWOW64\regsvr32.exe" set "REGSVR=%SystemRoot%\SysWOW64\regsvr32.exe"
"%REGSVR%" /s "%~2\%~3"
set "RC=%errorlevel%"
set "REGD=0"
reg query "HKCR\%~4\CLSID" >nul 2>&1 && set "REGD=1"
call :log yuanta_installed "component-%~5-regsvr32-%RC%-registered-%REGD%"
echo     regsvr32 exit %RC%   %~4 registered=%REGD%
exit /b 0

rem ===========================================================================
:run_vendor_elevated
net session >nul 2>&1
if not errorlevel 1 ( call :do_vendor & exit /b 0 )
echo.
echo Administrator rights are needed for the components [VC++, Yuanta OCX].
echo A UAC prompt will appear...
if "%DRYRUN%"=="1" ( echo   [dry-run] skipping elevation & exit /b 0 )
set "EV=%TEMP%\tfxq-elev-%RANDOM%.vbs"
> "%EV%" echo Set s = CreateObject("Shell.Application")
>> "%EV%" echo s.ShellExecute "%SELF%", "-VendorStep -UserLocalAppData ""%ULAD%"" -Log ""%LOGFILE%""", "%BUNDLE%", "runas", 1
cscript //nologo "%EV%"
set "ERC=%errorlevel%"
del "%EV%" >nul 2>&1
if not "%ERC%"=="0" goto run_vendor_declined
call :log elevated_dependency_run "rc-%ERC%"
exit /b 0
:run_vendor_declined
call :log elevated_dependency_declined "rc-%ERC%"
echo   [!] Components step skipped [no admin approval].
exit /b 1

rem ===========================================================================
:MODE_UNINSTALL
echo Uninstalling %APPNAME% ...
call :log uninstall_started "removedata-%REMOVEDATA%-removeyuanta-%REMOVEYUANTA%"
if "%DRYRUN%"=="1" goto uninst_data
set "DESKTOPDIR=%USERPROFILE%\Desktop"
for /f "tokens=2,*" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v Desktop 2^>nul ^| find "REG_"') do set "DESKTOPDIR=%%b"
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\%APPNAME%.lnk" del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\%APPNAME%.lnk" >nul 2>&1
if exist "%DESKTOPDIR%\%APPNAME%.lnk" del "%DESKTOPDIR%\%APPNAME%.lnk" >nul 2>&1
reg delete "%UNINSTKEY%" /f >nul 2>&1
if exist "%INSTALLDIR%" rmdir /s /q "%INSTALLDIR%"
:uninst_data
call :log app_files_removed "-"
if "%REMOVEDATA%"=="0" goto uninst_keepdata
echo   removing trading data %DATADIR%
if "%DRYRUN%"=="0" if exist "%DATADIR%" rmdir /s /q "%DATADIR%"
call :log data_removed "-"
goto uninst_yuanta_check
:uninst_keepdata
echo   keeping trading data %DATADIR%   pass -RemoveData to delete
:uninst_yuanta_check
if "%REMOVEYUANTA%"=="0" goto uninst_done
net session >nul 2>&1
if errorlevel 1 ( echo   [!] -RemoveYuanta needs admin; C:\Yuanta left in place. & goto uninst_done )
set "REGSVR=%SystemRoot%\System32\regsvr32.exe"
if exist "%SystemRoot%\SysWOW64\regsvr32.exe" set "REGSVR=%SystemRoot%\SysWOW64\regsvr32.exe"
if "%DRYRUN%"=="1" goto uninst_done
"%REGSVR%" /s /u "%YUANTA_API%\%OCX_TRADE%" 2>nul
"%REGSVR%" /s /u "%YUANTA_QAPI%\%OCX_QUOTE%" 2>nul
if exist "%YUANTA_API%"  rmdir /s /q "%YUANTA_API%"
if exist "%YUANTA_QAPI%" rmdir /s /q "%YUANTA_QAPI%"
call :log yuanta_removed "-"
:uninst_done
echo   done.
call :log run_finished "uninstall"
endlocal & exit /b 0

rem ===========================================================================
:summary
set "TR=not registered"
set "QU=not registered"
reg query "HKCR\%TRADE_PROGID%\CLSID" >nul 2>&1 && set "TR=registered"
reg query "HKCR\%QUOTE_PROGID%\CLSID" >nul 2>&1 && set "QU=registered"
set "VCSTATE=absent"
if exist "%BUNDLE%\vendor\vc_redist.x86.exe" set "VCSTATE=handled"
set "IDIR=%INSTALLDIR%"
if /i "%MODE%"=="deps" set "IDIR=skipped"
echo.
echo ==== summary ==========================================
echo   install dir  : %IDIR%
echo   data dir     : %DATADIR%
echo   VC++ x86     : %VCSTATE%
echo   Yuanta trade : %TR%
echo   Yuanta quote : %QU%
echo   log          : %LOGFILE%
echo ======================================================
exit /b 0

rem ===========================================================================
:make_shortcut
if "%DRYRUN%"=="1" ( echo   [dry-run] shortcut %~1 & exit /b 0 )
set "LV=%TEMP%\tfxq-lnk-%RANDOM%.vbs"
> "%LV%" echo Set w = CreateObject("WScript.Shell")
>> "%LV%" echo Set L = w.CreateShortcut(WScript.Arguments(0))
>> "%LV%" echo L.TargetPath = WScript.Arguments(1)
>> "%LV%" echo L.WorkingDirectory = WScript.Arguments(2)
>> "%LV%" echo L.Save
cscript //nologo "%LV%" "%~1" "%INSTALLDIR%\tfx-quant-desktop.cmd" "%INSTALLDIR%" >nul 2>&1
del "%LV%" >nul 2>&1
exit /b 0

rem ===========================================================================
:have_vendor
if exist "%BUNDLE%\vendor\vc_redist.x86.exe" exit /b 0
if exist "%BUNDLE%\vendor\API\%OCX_TRADE%" exit /b 0
if exist "%BUNDLE%\vendor\QAPI\%OCX_QUOTE%" exit /b 0
exit /b 1

rem ===========================================================================
:log
if not exist "%DATADIR%\logs" if "%DRYRUN%"=="0" mkdir "%DATADIR%\logs" 2>nul
>> "%LOGFILE%" echo {"ts":"%DATE:~-10% %TIME%","event":"%~1","detail":"%~2"}
exit /b 0

:FAIL
call :log run_failed "-"
echo.
echo FAILED. See %LOGFILE%
endlocal & exit /b 1
