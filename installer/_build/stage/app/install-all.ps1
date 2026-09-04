<#
.SYNOPSIS
  One-bundle installer for tfx-quant: the app, its runtime, and every dependency
  (Microsoft VC++ x86 redistributable + the Yuanta 交易/行情 OCX components).

.DESCRIPTION
  Runs from the root of a bundle produced by installer/build.py:

      install-all.ps1
      runtime\  Lib\  src\  launcher.pyw  tfx-quant-desktop.cmd
      build-manifest.json  SHA256SUMS  third_party_licenses.txt  RELEASE-NOTES-*.md
      vendor\  (vc_redist.x86.exe, API\, QAPI\)   <- present only in the "with components" bundle

  Non-admin steps (app files, data dirs, shortcuts, Add/Remove entry) run as you.
  The dependency steps (VC++ install, copy OCX to C:\Yuanta, regsvr32) need admin
  and trigger ONE UAC prompt. Decline it and the app still installs; run this
  script again later (or with -DependenciesOnly) to finish the components.

.PARAMETER InstallDir
  Where the app goes. Default: %LOCALAPPDATA%\Programs\tfx-quant (per-user, no admin).

.PARAMETER DependenciesOnly
  Only install VC++ + Yuanta components. Skip the app.

.PARAMETER AppOnly
  Only install the app. Skip VC++ + Yuanta components.

.PARAMETER Uninstall
  Remove the app, shortcuts and Add/Remove entry. Keeps %LOCALAPPDATA%\tfx_quant
  and C:\Yuanta unless -RemoveData / -RemoveYuanta are also given.

.PARAMETER DryRun
  Print what would happen; change nothing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-all.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-all.ps1 -DependenciesOnly
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
  [string]$InstallDir,
  [switch]$DependenciesOnly,
  [switch]$AppOnly,
  [switch]$NoShortcuts,
  [switch]$Uninstall,
  [switch]$RemoveData,
  [switch]$RemoveYuanta,
  [switch]$DryRun,
  # internal: the elevated re-entry that does only the admin dependency steps
  [switch]$VendorStep,
  [string]$BundleRoot,
  [string]$LogFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AppName          = 'tfx-quant'
$AppVersionFallbk = '0.0.0'
$DataDir          = Join-Path $env:LOCALAPPDATA 'tfx_quant'
$YuantaApiDir     = 'C:\Yuanta\API'
$YuantaQapiDir    = 'C:\Yuanta\QAPI'
$TradeProgId      = 'Yuanta.YuantaOrdCtrl.1'
$QuoteProgId      = 'YUANTAQUOTE.YuantaQuoteCtrl.1'
$UninstallKey     = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\tfx-quant'

if (-not $BundleRoot) { $BundleRoot = $PSScriptRoot }
$BundleRoot = (Resolve-Path -LiteralPath $BundleRoot).Path
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName" }
if (-not $LogFile) {
  $stamp   = (Get-Date -Format 'yyyyMMddHHmmss')
  $LogFile = Join-Path $DataDir "logs\install-all-$stamp.log"
}

# ---------------------------------------------------------------- helpers -----

function Write-Event {
  param([string]$Event, [hashtable]$Fields = @{})
  $rec = [ordered]@{ ts_utc = (Get-Date).ToUniversalTime().ToString('o'); event = $Event }
  foreach ($k in $Fields.Keys) { $rec[$k] = $Fields[$k] }
  try {
    $dir = Split-Path -Parent $LogFile
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    ($rec | ConvertTo-Json -Compress) | Add-Content -Path $LogFile -Encoding UTF8
  } catch { }
}

function Say {
  param([string]$Text, [string]$Color = 'Gray')
  Write-Host $Text -ForegroundColor $Color
}

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-RegSvr32 {
  $wow = Join-Path $env:SystemRoot 'SysWOW64\regsvr32.exe'   # 32-bit OCX on 64-bit OS
  if (Test-Path $wow) { return $wow }
  Join-Path $env:SystemRoot 'System32\regsvr32.exe'
}

function Test-ProgIdRegistered {
  param([string]$ProgId)
  Test-Path "Registry::HKEY_CLASSES_ROOT\$ProgId\CLSID"
}

function Get-AppVersion {
  $mf = Join-Path $BundleRoot 'build-manifest.json'
  if (Test-Path $mf) {
    try { return (Get-Content $mf -Raw | ConvertFrom-Json).app_version } catch { }
  }
  $AppVersionFallbk
}

function Copy-Bundle {
  param([string]$Source, [string]$Dest)
  # Everything, including install-all.ps1 itself (needed at $InstallDir for -Uninstall).
  $srcFull = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
  if ((Test-Path $Dest) -and ((Resolve-Path -LiteralPath $Dest).Path.TrimEnd('\') -ieq $srcFull)) {
    throw "already running from the install directory ($srcFull) - nothing to copy"
  }
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    $target = Join-Path $Dest $_.Name
    if ($DryRun) { Say "  would copy $($_.Name) -> $target"; return }
    if ($_.PSIsContainer) {
      Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
    } else {
      Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
  }
}

function New-Shortcut {
  param([string]$Path, [string]$Target, [string]$WorkDir)
  if ($DryRun) { Say "  would create shortcut $Path"; return }
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut($Path)
  $lnk.TargetPath       = $Target
  $lnk.WorkingDirectory = $WorkDir
  $lnk.Save()
}

# ------------------------------------------------------- dependency steps -----

function Install-VcRedist {
  $exe = Join-Path $BundleRoot 'vendor\vc_redist.x86.exe'
  if (-not (Test-Path $exe)) { Write-Event 'vcredist_absent'; return }
  Say "  Microsoft VC++ x86 runtime..." 'Cyan'
  if ($DryRun) { Say "  would run $exe /install /quiet /norestart"; return }
  $p = Start-Process $exe -ArgumentList '/install','/quiet','/norestart' -Wait -PassThru
  # 0 ok, 3010 ok (reboot later), 1638 ok (newer already present)
  $ok = @(0, 3010, 1638) -contains $p.ExitCode
  Write-Event 'vcredist_installed' @{ exit_code = $p.ExitCode; ok = $ok }
  Say ("    exit {0}{1}" -f $p.ExitCode, $(if ($ok) { ' (ok)' } else { ' (WARN)' })) $(if ($ok) { 'Green' } else { 'Yellow' })
}

function Install-YuantaComponent {
  param([string]$Name, [string]$SrcSub, [string]$DestDir, [string]$Ocx, [string]$ProgId)
  $src = Join-Path $BundleRoot "vendor\$SrcSub"
  if (-not (Test-Path (Join-Path $src $Ocx))) { Write-Event "yuanta_absent" @{ component = $Name }; return }
  Say "  Yuanta $Name -> $DestDir ..." 'Cyan'
  if ($DryRun) {
    Say "  would copy $src\* -> $DestDir and register $Ocx"
    return
  }
  New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
  Copy-Item -LiteralPath (Join-Path $src '*') -Destination $DestDir -Recurse -Force
  $regsvr = Get-RegSvr32
  $ocxPath = Join-Path $DestDir $Ocx
  $p = Start-Process $regsvr -ArgumentList '/s', "`"$ocxPath`"" -Wait -PassThru
  Start-Sleep -Milliseconds 300
  $registered = Test-ProgIdRegistered $ProgId
  Write-Event 'yuanta_installed' @{ component = $Name; regsvr32_exit = $p.ExitCode; registered = $registered }
  Say ("    regsvr32 exit {0}; {1} registered: {2}" -f $p.ExitCode, $ProgId, $registered) `
      $(if ($registered) { 'Green' } else { 'Yellow' })
}

function Invoke-DependencySteps {
  Write-Event 'dependencies_started' @{ elevated = (Test-Admin) }
  Install-VcRedist
  Install-YuantaComponent -Name '交易 API' -SrcSub 'API'  -DestDir $YuantaApiDir  -Ocx 'YuantaOrd.ocx'              -ProgId $TradeProgId
  Install-YuantaComponent -Name '行情 API' -SrcSub 'QAPI' -DestDir $YuantaQapiDir -Ocx 'YuantaQuote_v2.1.2.9.ocx'   -ProgId $QuoteProgId
  Write-Event 'dependencies_finished'
}

function Need-DependencySteps {
  (Test-Path (Join-Path $BundleRoot 'vendor\vc_redist.x86.exe')) -or
  (Test-Path (Join-Path $BundleRoot 'vendor\API\YuantaOrd.ocx'))  -or
  (Test-Path (Join-Path $BundleRoot 'vendor\QAPI\YuantaQuote_v2.1.2.9.ocx'))
}

function Invoke-ElevatedDependencySteps {
  if (Test-Admin) { Invoke-DependencySteps; return $true }
  Say "`n需要系統管理員權限安裝相依元件（VC++、元大 OCX）— 會出現 UAC 提示..." 'Yellow'
  if ($DryRun) { Say "  (dry run: skipping elevation)"; return $true }
  $args = @('-NoProfile','-ExecutionPolicy','Bypass','-File', "`"$PSCommandPath`"",
            '-VendorStep', '-BundleRoot', "`"$BundleRoot`"", '-LogFile', "`"$LogFile`"")
  try {
    $p = Start-Process powershell -Verb RunAs -ArgumentList $args -Wait -PassThru
    Write-Event 'elevated_dependency_run' @{ exit_code = $p.ExitCode }
    return ($p.ExitCode -eq 0)
  } catch {
    Write-Event 'elevated_dependency_declined' @{ error = $_.Exception.Message }
    Say "  已略過相依元件安裝（未取得系統管理員授權）。" 'Yellow'
    Say "  稍後可執行： powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -DependenciesOnly" 'Yellow'
    return $false
  }
}

# --------------------------------------------------------------- app steps ----

function Install-App {
  $version = Get-AppVersion
  Say "`n安裝 $AppName $version -> $InstallDir" 'Cyan'
  foreach ($sub in 'config','logs','backup','data') {
    $d = Join-Path $DataDir $sub
    if ($DryRun) { Say "  would ensure $d" }
    elseif (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
  }
  Copy-Bundle -Source $BundleRoot -Dest $InstallDir
  Write-Event 'app_files_installed' @{ install_dir = $InstallDir; version = $version }

  if (-not $NoShortcuts) {
    $cmd = Join-Path $InstallDir 'tfx-quant-desktop.cmd'
    $sm  = Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"
    $dt  = Join-Path ([Environment]::GetFolderPath('Desktop'))  "$AppName.lnk"
    New-Shortcut -Path $sm -Target $cmd -WorkDir $InstallDir
    New-Shortcut -Path $dt -Target $cmd -WorkDir $InstallDir
    Write-Event 'shortcuts_created'
  }

  if (-not $DryRun) {
    New-Item -Path $UninstallKey -Force | Out-Null
    $unins = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\install-all.ps1`" -Uninstall"
    Set-ItemProperty $UninstallKey DisplayName     "$AppName $version"
    Set-ItemProperty $UninstallKey DisplayVersion  $version
    Set-ItemProperty $UninstallKey Publisher       'tfx-quant'
    Set-ItemProperty $UninstallKey InstallLocation $InstallDir
    Set-ItemProperty $UninstallKey UninstallString $unins
    Set-ItemProperty $UninstallKey QuietUninstallString $unins
    Set-ItemProperty $UninstallKey NoModify 1 -Type DWord
    Set-ItemProperty $UninstallKey NoRepair 1 -Type DWord
    Write-Event 'addremove_entry_written'
  }
  Say "  完成。" 'Green'
}

function Uninstall-App {
  Say "解除安裝 $AppName ..." 'Cyan'
  Write-Event 'uninstall_started' @{ remove_data = [bool]$RemoveData; remove_yuanta = [bool]$RemoveYuanta }

  Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, 'OrdinalIgnoreCase') } |
    ForEach-Object { if (-not $DryRun) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }

  foreach ($lnk in @(
      (Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"),
      (Join-Path ([Environment]::GetFolderPath('Desktop'))  "$AppName.lnk"))) {
    if ((Test-Path $lnk) -and -not $DryRun) { Remove-Item $lnk -Force -ErrorAction SilentlyContinue }
  }
  if ((Test-Path $UninstallKey) -and -not $DryRun) { Remove-Item $UninstallKey -Recurse -Force }
  if ((Test-Path $InstallDir) -and -not $DryRun) { Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue }
  Write-Event 'app_files_removed'

  if ($RemoveData) {
    Say "  刪除交易資料 $DataDir" 'Yellow'
    if ((Test-Path $DataDir) -and -not $DryRun) { Remove-Item $DataDir -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Event 'data_removed'
  } else {
    Say "  保留交易資料 $DataDir（加 -RemoveData 才刪除）" 'Gray'
  }

  if ($RemoveYuanta) {
    if (-not (Test-Admin) -and -not $DryRun) {
      Say "  需要系統管理員權限移除 C:\Yuanta 註冊 — 略過（請以系統管理員重跑並加 -RemoveYuanta）" 'Yellow'
    } else {
      $regsvr = Get-RegSvr32
      foreach ($o in @("$YuantaApiDir\YuantaOrd.ocx", "$YuantaQapiDir\YuantaQuote_v2.1.2.9.ocx")) {
        if ((Test-Path $o) -and -not $DryRun) {
          Start-Process $regsvr -ArgumentList '/s','/u',"`"$o`"" -Wait | Out-Null
        }
      }
      foreach ($d in @($YuantaApiDir, $YuantaQapiDir)) {
        if ((Test-Path $d) -and -not $DryRun) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue }
      }
      Write-Event 'yuanta_removed'
    }
  }
  Say "  完成。" 'Green'
}

# ------------------------------------------------------------------- main -----

Write-Event 'run_started' @{
  bundle_root = $BundleRoot
  mode        = $(if ($Uninstall) { 'uninstall' } elseif ($VendorStep) { 'vendor_step' }
                  elseif ($DependenciesOnly) { 'deps_only' } elseif ($AppOnly) { 'app_only' } else { 'full' })
  dry_run     = [bool]$DryRun
}

try {
  if ($VendorStep) {
    # elevated re-entry: only the dependency steps
    Invoke-DependencySteps
    Write-Event 'run_finished' @{ exit_code = 0 }
    exit 0
  }

  if ($Uninstall) {
    Uninstall-App
    Write-Event 'run_finished' @{ exit_code = 0 }
    exit 0
  }

  $depsOk = $true
  if (-not $AppOnly -and (Need-DependencySteps)) {
    $depsOk = Invoke-ElevatedDependencySteps
  } elseif (-not $AppOnly) {
    Say "此 bundle 不含相依元件（純程式版）— 請依安裝手冊自行安裝元大 API 與 VC++。" 'Yellow'
  }

  if (-not $DependenciesOnly) {
    Install-App
  }

  Say ""
  Say "==== 摘要 ============================================" 'White'
  Say ("  安裝目錄 : {0}" -f $(if ($DependenciesOnly) { '(略過)' } else { $InstallDir }))
  Say ("  資料目錄 : {0}" -f $DataDir)
  Say ("  VC++ x86 : {0}" -f $(if (Test-Path (Join-Path $BundleRoot 'vendor\vc_redist.x86.exe')) { '已處理' } else { '未含' }))
  Say ("  元大交易 : {0}" -f $(if (Test-ProgIdRegistered $TradeProgId) { '已註冊' } else { '未註冊' })) `
      $(if (Test-ProgIdRegistered $TradeProgId) { 'Green' } else { 'Yellow' })
  Say ("  元大行情 : {0}" -f $(if (Test-ProgIdRegistered $QuoteProgId) { '已註冊' } else { '未註冊' })) `
      $(if (Test-ProgIdRegistered $QuoteProgId) { 'Green' } else { 'Yellow' })
  Say ("  記錄檔   : {0}" -f $LogFile)
  Say "=====================================================" 'White'
  if (-not $depsOk) {
    Say "`n相依元件尚未完成安裝。重跑： powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -DependenciesOnly" 'Yellow'
  }
  Write-Event 'run_finished' @{ exit_code = 0; dependencies_ok = $depsOk }
  exit 0
}
catch {
  Write-Event 'run_failed' @{ error = $_.Exception.Message }
  Say "`n失敗： $($_.Exception.Message)" 'Red'
  Say "記錄檔： $LogFile" 'Red'
  exit 1
}
