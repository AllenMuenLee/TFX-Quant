; tfx-quant Windows installer (Inno Setup 6).
;
; Compiled by installer/make_installer.py, which passes:
;   /DAppVersion=<x.y.z>  /DStageApp=<...\stage\app>  /DOutputDir=<...\dist>
;
; Design notes (see docs/installation-manual.md, docs/maintenance.md):
;  * The tfx-quant app installs per-user with no administrator rights.
;  * When the build bundled a vendor payload (stage/app/vendor exists ->
;    make_installer.py passes /DBundleVendor), the finish step runs
;    "{app}\install-all.bat -VendorStep" once elevated (one UAC prompt): installs
;    the Microsoft VC++ x86 redistributable and copies + registers the Yuanta
;    交易/行情 OCX to C:\Yuanta. Declining -> re-run "{app}\install-all.bat
;    -DependenciesOnly" as admin later. install-all.bat is the single dependency
;    implementation, shared with the "copy the folder + run the .bat" install
;    path. The Yuanta components are Yuanta's property -- bundling them requires a
;    redistribution right, asserted by whoever produced the build.
;  * Blocking pre-checks (Windows version, disk space) are done here in Pascal.
;  * Upgrades stop a running instance (AppMutex), back up every SQLite database and
;    run an integrity check via the *previous* build's bundled Python before any
;    file is replaced; a failed check aborts the upgrade with the old version intact.
;  * Uninstall keeps %LOCALAPPDATA%\tfx_quant AND C:\Yuanta by default.
;  * Code signing is applied to the finished .exe by make_installer.py (opt-in).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageApp
  #define StageApp "_build\stage\app"
#endif
#ifndef OutputDir
  #define OutputDir "_build\dist"
#endif

#define AppName "tfx-quant"
#define AppPublisher "tfx-quant"
; Must match desktop/composition.py + desktop/__main__.py, which read %LOCALAPPDATA%.
#define DataDir "{localappdata}\tfx_quant"

[Setup]
AppId={{7F3C2A1E-6B4D-4E88-9C21-0A1B2C3D4E5F}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=
OutputDir={#OutputDir}
; Not literally "setup" — Windows AppCompat shims every setup.exe to load version.dll
; etc. unsafely (DLL-hijack surface). "tfx-quant-setup" keeps the setup-exe identity
; without that. make_installer.py knows this name.
OutputBaseFilename=tfx-quant-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AppMutex=tfx_quant_desktop_singleton
UninstallDisplayName={#AppName} {#AppVersion}
MinVersion=10.0

[Languages]
Name: "zhhant"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#StageApp}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
Name: "{#DataDir}"
Name: "{#DataDir}\config"
Name: "{#DataDir}\logs"
Name: "{#DataDir}\backup"
Name: "{#DataDir}\data"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\tfx-quant-desktop.cmd"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\tfx-quant-desktop.cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"; Flags: unchecked
#ifdef BundleVendor
Name: "vendorinstall"; \
  Description: "安裝並註冊元大 API 元件與 Microsoft VC++ x86 執行環境（需要系統管理員權限，會出現一次 UAC 提示）"
#endif

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
const
  MIN_FREE_MB = 400;

var
  UpgradeLogPath: string;
  UninstRemoveData: Boolean;

function InstallLog(): string;
begin
  Result := ExpandConstant('{#DataDir}\logs\installer-') +
            GetDateTimeString('yyyymmddhhnnss', #0, #0) + '.log';
end;

function GetInstallLog(Param: string): string;
begin
  if UpgradeLogPath = '' then
    UpgradeLogPath := InstallLog();
  Result := UpgradeLogPath;
end;

procedure LogEvent(const LogPath, EventName, Extra: string);
var
  fh: string;
begin
  fh := LogPath;
  ForceDirectories(ExtractFileDir(fh));
  SaveStringToFile(fh,
    '{"ts":"' + GetDateTimeString('yyyy/mm/dd hh:nn:ss', #0, #0) +
    '","event":"' + EventName + '"' + Extra + '}' + #13#10, True);
end;

function DriveFreeMB(const Path: string): Int64;
var
  FreeBytes, TotalBytes: Int64;
begin
  if GetSpaceOnDisk64(Path, FreeBytes, TotalBytes) then
    Result := FreeBytes div 1048576
  else
    Result := -1;
end;

function VCRedistX86Present(): Boolean;
var
  v: Cardinal;
begin
  Result :=
    RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86', 'Installed', v) and (v = 1);
end;

function YuantaTradeApiLooksInstalled(): Boolean;
begin
  Result := FileExists('C:\Yuanta\API\YuantaOrd.ocx') and
            RegKeyExists(HKCR, 'Yuanta.YuantaOrdCtrl.1\CLSID');
end;

function YuantaQuoteApiLooksInstalled(): Boolean;
begin
  Result := FileExists('C:\Yuanta\QAPI\YuantaQuote_v2.1.2.9.ocx') and
            RegKeyExists(HKCR, 'YUANTAQUOTE.YuantaQuoteCtrl.1\CLSID');
end;

function InitializeSetup(): Boolean;
var
  freeMB: Int64;
  warn: string;
begin
  Result := True;
  freeMB := DriveFreeMB(ExpandConstant('{autopf}'));
  if (freeMB >= 0) and (freeMB < MIN_FREE_MB) then
  begin
    MsgBox(Format('安裝需要至少 %d MB 可用空間，目前僅 %d MB。', [MIN_FREE_MB, freeMB]),
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  warn := '';
  if not VCRedistX86Present() then
    warn := warn + '- 找不到 Microsoft Visual C++ 2015-2022 (x86) 可轉散發套件。' + #13#10;
  if not YuantaTradeApiLooksInstalled() then
    warn := warn + '- 尚未偵測到已註冊的元大「交易」API（正式環境下單前必須另外安裝）。' + #13#10;
  if not YuantaQuoteApiLooksInstalled() then
    warn := warn + '- 尚未偵測到已註冊的元大「行情」API（即時行情必須另外安裝）。' + #13#10;

  if warn <> '' then
    MsgBox('安裝可以繼續，但請注意以下項目（詳見安裝手冊）：'#13#10#13#10
           + warn + #13#10
           + '這些元件由券商另外提供，需以系統管理員身分執行其 install_*.bat。',
           mbInformation, MB_OK);
end;

// --- Upgrade safety: stop -> backup -> integrity check, before files change ---
function PrepareToInstall(var NeedsRestart: Boolean): string;
var
  prevPython: string;
  resultCode: Integer;
begin
  Result := '';
  UpgradeLogPath := InstallLog();
  LogEvent(UpgradeLogPath, 'run_started',
    ',"app_version":"{#AppVersion}","phase":"installer"');

  prevPython := ExpandConstant('{app}\runtime\python.exe');
  if not FileExists(prevPython) then
  begin
    LogEvent(UpgradeLogPath, 'fresh_install', '');
    Exit;
  end;

  { An upgrade from a build older than Feature 16 has no migrate module. Skip the
    safety step with a warning rather than aborting — that build also had no
    schema this one could not read. }
  if not Exec(prevPython, '-c "import tfx_quant.packaging.migrate"',
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, resultCode)
     or (resultCode <> 0) then
  begin
    LogEvent(UpgradeLogPath, 'upgrade_backup_skipped', ',"reason":"no_migrate_module_in_previous_build"');
    Exit;
  end;

  LogEvent(UpgradeLogPath, 'upgrade_backup_started', '');
  if not Exec(prevPython,
       '-m tfx_quant.packaging.migrate --apply --log "' + UpgradeLogPath + '"',
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, resultCode) then
  begin
    Result := '無法執行升級前資料備份／檢查，已停止升級以保留舊版本。';
    LogEvent(UpgradeLogPath, 'upgrade_aborted', ',"reason":"exec_failed"');
    Exit;
  end;

  LogEvent(UpgradeLogPath, 'upgrade_backup_finished', ',"exit_code":' + IntToStr(resultCode));
  if resultCode <> 0 then
  begin
    Result := '升級前資料完整性檢查失敗（結束碼 ' + IntToStr(resultCode) + '）。' + #13#10 +
      '已停止升級，舊版本與資料保持不變。備份位於 %LOCALAPPDATA%\tfx_quant\backup。' + #13#10 +
      '如需協助請提供 ' + UpgradeLogPath + '。';
    LogEvent(UpgradeLogPath, 'upgrade_aborted', ',"reason":"integrity_check_failed"');
  end;
end;

procedure RunVendorInstall();
var
  cmd, script, params, logPath, lad: string;
  resultCode: Integer;
begin
  script := ExpandConstant('{app}\install-all.bat');
  if not FileExists(script) then
  begin
    LogEvent(UpgradeLogPath, 'vendor_install_skipped', ',"reason":"no_payload"');
    Exit;
  end;
  logPath := ExpandConstant('{#DataDir}\logs\vendor-install-') +
             GetDateTimeString('yyyymmddhhnnss', #0, #0) + '.log';
  ForceDirectories(ExtractFileDir(logPath));
  lad := ExpandConstant('{localappdata}');
  cmd := ExpandConstant('{sys}\cmd.exe');
  // {localappdata} is the *installing user's* here (PrivilegesRequired=lowest); pass it
  // so the elevated child logs to the right profile, not the admin's.
  params := '/c ""' + script + '" -VendorStep -UserLocalAppData "' + lad +
            '" -Log "' + logPath + '""';
  LogEvent(UpgradeLogPath, 'vendor_install_started', '');
  // 'runas' -> one UAC prompt; regsvr32 + copy to C:\Yuanta + vc_redist all need admin.
  if ShellExec('runas', cmd, params, ExpandConstant('{app}'),
       SW_SHOW, ewWaitUntilTerminated, resultCode) then
    LogEvent(UpgradeLogPath, 'vendor_install_finished',
             ',"exit_code":' + IntToStr(resultCode))
  else
  begin
    LogEvent(UpgradeLogPath, 'vendor_install_declined', '');
    MsgBox(
      '未安裝元大 API 元件與 VC++ 執行環境（可能是取消了系統管理員授權）。' + #13#10#13#10
      + '稍後可用系統管理員身分執行：' + #13#10
      + '  ' + script + ' -DependenciesOnly' + #13#10#13#10
      + '或依安裝手冊手動安裝。程式仍可先以「模擬」環境啟動。',
      mbInformation, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  py: string;
  resultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    LogEvent(UpgradeLogPath, 'files_installed', ',"install_dir_present":true');

#ifdef BundleVendor
    if WizardIsTaskSelected('vendorinstall') then
      RunVendorInstall();
#endif

    // Informational environment report -> the installer log (never blocks).
    py := ExpandConstant('{app}\runtime\python.exe');
    if FileExists(py) then
      Exec(py, '-m tfx_quant.packaging.prechecks --log "' + GetInstallLog('') + '"',
           ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, resultCode);

    LogEvent(UpgradeLogPath, 'run_finished', ',"exit_code":0,"rollback_result":"none"');
  end;
end;

// --- Uninstall: KEEP trading data by default. ---
// The data directory (%LOCALAPPDATA%\tfx_quant: databases, audit, logs, settings) is
// only ever removed on an EXPLICIT, unambiguous request:
//   * interactive uninstall: the operator answers Yes to the prompt (default No), OR
//   * any uninstall: /REMOVEUSERDATA is passed on the command line.
// A silent uninstall with no flag NEVER touches the data directory — a suppressed
// message box must not be able to answer "delete my data" for the operator.
function WantsDataRemoval(): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/REMOVEUSERDATA') = 0 then
    begin
      Result := True;
      Exit;
    end;
  if not UninstallSilent() then
    Result := MsgBox(
      '是否一併刪除交易資料、log、備份與設定（%LOCALAPPDATA%\tfx_quant）？' + #13#10
      + '預設為「否」，保留資料以便日後重新安裝或稽核。',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure InitializeUninstallProgressForm();
begin
  UninstRemoveData := WantsDataRemoval();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and UninstRemoveData then
    DelTree(ExpandConstant('{#DataDir}'), True, True, True);
end;
