[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$DryRun,
    [string]$Python = "",
    [string]$Node = "",
    [string]$OutboundProxy = "",
    [ValidateRange(1, 65535)][int]$ProxyPort = 13457,
    [ValidateRange(1, 65535)][int]$CoordinatorPort = 17893,
    [string]$WslDistribution = "Ubuntu-22.04",
    [int]$WslPort = 2222,
    [string[]]$WslReclaimPath = @(),
    [string[]]$WindowsReclaimPath = @(),
    [string[]]$WindowsWorktreeRepo = @(),
    [string[]]$WindowsWorktreeRoot = @(),
    [ValidateRange(1, 365)][double]$WorktreeMaxAgeDays = 14
)

# Windows/Desktop installer. It is deliberately preview-only when no explicit
# action is supplied. Running this script without -Install never writes a file,
# starts a process, changes the proxy, or touches Claude settings.
$ErrorActionPreference = "Stop"
if ($Install -and $Uninstall) {
    throw "Choose -Install or -Uninstall, not both."
}
if ($ProxyPort -eq $CoordinatorPort) {
    throw "ProxyPort and CoordinatorPort must be different."
}
if (-not ($Install -or $Uninstall)) {
    $Install = $true
    $DryRun = $true
}

$Repo = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$AppDataRoot = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME "AppData\Roaming" }
$LocalAppDataRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
$InstallRoot = Join-Path $LocalAppDataRoot "Quotapool"
$Venv = Join-Path $InstallRoot "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvPythonw = Join-Path $Venv "Scripts\pythonw.exe"
$FleetExe = Join-Path $Venv "Scripts\quotapool.exe"
$WrapperExe = Join-Path $Venv "Scripts\quotapool-desktop-wrapper.exe"
$DesktopCodeShimExe = Join-Path $Venv "Scripts\quotapool-desktop-code.exe"
$ConfigDir = Join-Path $AppDataRoot "quotapool"
$StateDir = Join-Path $InstallRoot "state"
$ShimDir = Join-Path $InstallRoot "shims"
$Runtime = Join-Path $ConfigDir "runtime.json"
$TeamConfig = Join-Path $ConfigDir "teamclaude.json"
$Db = Join-Path $StateDir "fleet.sqlite3"
$PidFile = Join-Path $StateDir "desktop-services.json"
$Settings = Join-Path $HOME ".claude\settings.json"
$TrayTaskName = "QuotaPool Tray Monitor"
$ServiceTaskName = "QuotaPool Service Watchdog"
$DiskTaskName = "QuotaPool Disk Maintenance"
$WorktreeTaskName = "QuotaPool Worktree Maintenance"
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Claude (QuotaPool).lnk"
$StartMenuShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Claude (QuotaPool).lnk"

function Find-CommandPath([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Read-ProgramVersion([string]$Path, [string]$Argument) {
    try {
        $value = (& $Path $Argument 2>$null | Select-Object -First 1)
        if ($value) { return [string]$value }
    } catch {}
    return "unknown"
}

function Find-DesktopCodeBinary {
    $roots = @(
        (Join-Path $AppDataRoot "Claude\claude-code"),
        (Join-Path $LocalAppDataRoot "Claude\claude-code")
    ) | Select-Object -Unique
    $items = @()
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $binary = Join-Path $_.FullName "claude.exe"
                if (Test-Path -LiteralPath $binary -PathType Leaf) {
                    $items += [pscustomobject]@{
                        Version = $_.Name
                        Binary = $binary
                    }
                }
            }
    }
    return $items | Sort-Object {
        try { [version]$_.Version } catch { [version]"0.0" }
    } -Descending | Select-Object -First 1
}

function Detect-OutboundProxy {
    if ($OutboundProxy) { return $OutboundProxy.Trim() }
    foreach ($name in @("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { return $value.Trim() }
    }
    try {
        $internet = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        if ($internet.ProxyEnable -and $internet.ProxyServer) {
            $raw = [string]$internet.ProxyServer
            $https = ($raw -split ';' | Where-Object { $_ -match '^https=' } | Select-Object -First 1)
            if ($https) { $raw = ($https -split '=', 2)[1] }
            elseif ($raw -match ';') {
                $http = ($raw -split ';' | Where-Object { $_ -match '^http=' } | Select-Object -First 1)
                if ($http) { $raw = ($http -split '=', 2)[1] }
            }
            if ($raw -and $raw -notmatch '^[a-z][a-z0-9+.-]*://') {
                $raw = "http://$raw"
            }
            return $raw
        }
    } catch {}
    return ""
}

function Say-Plan([string]$Text) {
    Write-Host "  PLAN: $Text" -ForegroundColor DarkGray
}

function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $File" }
}

$pythonPath = if ($Python) { $Python } else { Find-CommandPath "python" }
$nodePath = if ($Node) { $Node } else { Find-CommandPath "node" }
$desktop = Find-DesktopCodeBinary
$proxy = Detect-OutboundProxy

Write-Host "Quotapool Windows/Desktop preflight"
Write-Host "  Repository : $Repo"
Write-Host "  Python     : $(if ($pythonPath) { Read-ProgramVersion $pythonPath '--version' } else { 'missing' })"
Write-Host "  Node       : $(if ($nodePath) { Read-ProgramVersion $nodePath '--version' } else { 'missing' })"
Write-Host "  Desktop    : $(if ($desktop) { "$($desktop.Version) -> $($desktop.Binary)" } else { 'not found' })"
Write-Host "  Outbound   : $(if ($proxy) { 'configured (value hidden)' } else { 'direct / not detected' })"

if (-not $pythonPath) { throw "Python 3.9+ was not found." }
if (-not $nodePath) { throw "Node.js 20+ was not found." }
if (-not $desktop) { throw "Claude Desktop's managed Claude Code binary was not found." }
$pythonMajorMinor = (& $pythonPath -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null).Trim()
try { $pythonVersion = [version]($pythonMajorMinor + '.0') } catch { throw "Could not read Python version." }
if ($pythonVersion -lt [version]'3.9.0') { throw "Python 3.9+ is required (found $pythonMajorMinor)." }
$nodeMajor = (& $nodePath -p "process.versions.node.split('.')[0]" 2>$null).Trim()
if (-not $nodeMajor -or [int]$nodeMajor -lt 20) { throw "Node.js 20+ is required (found $(Read-ProgramVersion $nodePath '--version'))." }

if ($Uninstall) {
    if (-not $DryRun -and (Test-Path -LiteralPath $VenvPython)) {
        & $VenvPython -m claude_fleet.tray --runtime $Runtime --shutdown 2>$null
    }
    foreach ($taskName in @($TrayTaskName, $ServiceTaskName, $DiskTaskName, $WorktreeTaskName)) {
        if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) { continue }
        if ($DryRun) { Say-Plan "remove scheduled task '$taskName'" }
        else { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    }
    foreach ($shortcut in @($DesktopShortcut, $StartMenuShortcut)) {
        if (-not (Test-Path -LiteralPath $shortcut)) { continue }
        if ($DryRun) { Say-Plan "remove shortcut '$shortcut'" }
        else { Remove-Item -LiteralPath $shortcut -Force }
    }
    if (-not (Test-Path -LiteralPath $FleetExe)) {
        Write-Warning "Quotapool virtual environment not found; nothing was changed."
        exit 0
    }
    if ($DryRun) {
        Say-Plan "quotapool settings remove --settings `"$Settings`" --wrapper `"$WrapperExe`" --fleet-binary `"$FleetExe`" --state-db `"$Db`""
        Say-Plan "quotapool-desktop stop (only processes recorded by Quotapool are eligible)"
        Say-Plan "stop the tray monitor without stopping Claude tasks"
        Say-Plan "preserve OAuth credentials, runtime.json, and fleet.sqlite3"
        exit 0
    }
    $env:FLEET_RUNTIME_CONFIG = $Runtime
    Invoke-Checked (Join-Path $Venv "Scripts\quotapool-desktop.exe") @("--runtime", $Runtime, "uninstall-code-shim")
    Invoke-Checked $FleetExe @("settings", "remove", "--settings", $Settings, "--wrapper", $WrapperExe, "--fleet-binary", $FleetExe, "--state-db", $Db)
    if ([Environment]::GetEnvironmentVariable("CLAUDE_CODE_PROCESS_WRAPPER", "User") -eq $WrapperExe) {
        [Environment]::SetEnvironmentVariable("CLAUDE_CODE_PROCESS_WRAPPER", $null, "User")
    }
    Invoke-Checked (Join-Path $Venv "Scripts\quotapool-desktop.exe") @("stop")
    if (Test-Path -LiteralPath $ShimDir) { Remove-Item -LiteralPath $ShimDir -Recurse -Force }
    Write-Host "Settings and Quotapool shims removed. Credentials/state were preserved."
    exit 0
}

if ($DryRun) {
    Say-Plan "create $Venv and install this checkout into the private venv"
    Say-Plan "write runtime.json/teamclaude.json under $ConfigDir (OAuth accounts preserved)"
    Say-Plan "create Windows claude.cmd shim under $ShimDir"
    Say-Plan "back up Desktop's managed claude.exe and install a reversible QuotaPool trampoline"
    Say-Plan "create quiet Claude (QuotaPool) shortcuts on Desktop and Start menu"
    Say-Plan "bind the Windows-local data plane to 127.0.0.1:$ProxyPort and coordinator to 127.0.0.1:$CoordinatorPort"
    Say-Plan "register '$TrayTaskName' at user logon and start the tray monitor"
    Say-Plan "register '$ServiceTaskName' every minute to restore local services"
    Say-Plan "register '$DiskTaskName' every 2 minutes with safe Windows/WSL cleanup guards"
    Say-Plan "register '$WorktreeTaskName' daily; remove only old clean worktrees whose HEAD is recoverable from another ref"
    Say-Plan "quotapool settings install --settings `"$Settings`" --wrapper `"$WrapperExe`" --fleet-binary `"$FleetExe`" --state-db `"$Db`""
    Say-Plan "do not modify the WindowsApps Claude package, system proxy, certificates, or running processes"
    exit 0
}

New-Item -ItemType Directory -Force -Path $InstallRoot, $ConfigDir, $StateDir, $ShimDir | Out-Null
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Invoke-Checked $pythonPath @("-m", "venv", $Venv)
}
Invoke-Checked $VenvPython @("-m", "pip", "install", "-e", $Repo)

$shim = "@echo off`r`n`"$FleetExe`" run -- %*`r`n"
[IO.File]::WriteAllText((Join-Path $ShimDir "claude.cmd"), $shim, [Text.UTF8Encoding]::new($false))
$token = (& $VenvPython -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$bootstrap = Join-Path $Repo "scripts\bootstrap_config.py"
$bootstrapArgs = @(
    $bootstrap, "--config-dir", $ConfigDir, "--state-dir", $StateDir,
    "--prefix", $Repo, "--claude-binary", $desktop.Binary,
    "--token=$token", "--node-binary", $nodePath, "--shim-dir", $ShimDir,
    "--desktop-wrapper", $WrapperExe, "--desktop-pid-file", $PidFile,
    "--proxy-port=$ProxyPort", "--coordinator-port=$CoordinatorPort",
    "--ca-path=$(Join-Path $ConfigDir 'teamclaude-ca.pem')"
)
if ($proxy) { $bootstrapArgs += @("--outbound-proxy", $proxy) }
Invoke-Checked $VenvPython $bootstrapArgs
$env:FLEET_RUNTIME_CONFIG = $Runtime
Invoke-Checked $FleetExe @("settings", "install", "--settings", $Settings, "--wrapper", $WrapperExe, "--fleet-binary", $FleetExe, "--state-db", $Db)
Invoke-Checked (Join-Path $Venv "Scripts\quotapool-desktop.exe") @(
    "--runtime", $Runtime, "install-code-shim", "--managed", $desktop.Binary,
    "--source", $DesktopCodeShimExe
)
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_PROCESS_WRAPPER", $WrapperExe, "User")
$desktopApp = Get-AppxPackage -Name Claude -ErrorAction SilentlyContinue |
    Sort-Object Version -Descending | Select-Object -First 1
$desktopExecutable = if ($desktopApp) { Join-Path $desktopApp.InstallLocation "app\Claude.exe" } else { "" }
$shortcutShell = New-Object -ComObject WScript.Shell
foreach ($shortcutPath in @($DesktopShortcut, $StartMenuShortcut)) {
    $shortcutDir = Split-Path $shortcutPath -Parent
    New-Item -ItemType Directory -Force -Path $shortcutDir | Out-Null
    $shortcut = $shortcutShell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $VenvPythonw
    $shortcut.Arguments = "-m claude_fleet.desktop --runtime `"$Runtime`" launch"
    $shortcut.WorkingDirectory = $InstallRoot
    $shortcut.Description = "Start Claude Desktop through QuotaPool"
    if ($desktopExecutable -and (Test-Path -LiteralPath $desktopExecutable)) {
        $shortcut.IconLocation = "$desktopExecutable,0"
    }
    $shortcut.Save()
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trayAction = New-ScheduledTaskAction -Execute $VenvPythonw -Argument "-m claude_fleet.tray --runtime `"$Runtime`""
$trayTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$trayPrincipal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$traySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TrayTaskName -Action $trayAction -Trigger $trayTrigger -Principal $trayPrincipal -Settings $traySettings -Description "QuotaPool account, service, WSL and quota status monitor" -Force | Out-Null
Start-ScheduledTask -TaskName $TrayTaskName

$serviceAction = New-ScheduledTaskAction -Execute $VenvPythonw -Argument "-m claude_fleet.desktop --runtime `"$Runtime`" start"
$serviceTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$serviceSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $ServiceTaskName -Action $serviceAction -Trigger $serviceTrigger -Principal $trayPrincipal -Settings $serviceSettings -Description "QuotaPool local coordinator and data-plane watchdog" -Force | Out-Null
Start-ScheduledTask -TaskName $ServiceTaskName

$wslHome = ""
if ($WslReclaimPath.Count -eq 0) {
    $wsl = Join-Path $env:WINDIR "System32\wsl.exe"
    try {
        $wslHome = (& $wsl -d $WslDistribution --exec /bin/sh -lc 'printf %s "$HOME"' 2>$null).Trim()
    } catch { $wslHome = "" }
    if ($wslHome -and $wslHome.StartsWith("/")) {
        $WslReclaimPath = @(
            "$wslHome/.cargo-target-*",
            "$wslHome/.cargo-shared-target",
            "$wslHome/projects/.evox-target-shared",
            "$wslHome/projects/.evox-target-*",
            "$wslHome/projects/*/.claude/worktrees/*/target",
            "$wslHome/projects/*/target"
        )
    }
}
$diskState = Join-Path $StateDir "disk-maintenance.json"
$diskArguments = "-m claude_fleet.wsl_guard maintain --distribution `"$WslDistribution`" --port $WslPort --state `"$diskState`""
$fixedDrives = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction SilentlyContinue
if ($WindowsReclaimPath.Count -eq 0) {
    foreach ($drive in $fixedDrives) {
        foreach ($relative in @(
            "EvoMap_projects\evox\target",
            "EvoMap_projects\evox\target-*",
            "EvoMap_projects\evox-worktrees\*\target",
            "EvoMap_projects\evox\.claude\worktrees\*\target"
        )) {
            $pattern = Join-Path ($drive.DeviceID + "\") $relative
            if (Test-Path -Path $pattern -PathType Container) {
                $WindowsReclaimPath += $pattern
            }
        }
    }
}
$wslSshUser = if ($wslHome) { Split-Path $wslHome -Leaf } else { "" }
$wslIdentity = Join-Path $HOME ".ssh\quotapool-wsl-desktop"
if ($wslSshUser -and (Test-Path -LiteralPath $wslIdentity -PathType Leaf)) {
    $diskArguments += " --ssh-user `"$wslSshUser`" --identity `"$wslIdentity`""
}
foreach ($reclaimPath in $WslReclaimPath) {
    if ($reclaimPath) { $diskArguments += " --reclaim-path `"$reclaimPath`"" }
}
foreach ($reclaimPath in $WindowsReclaimPath) {
    if ($reclaimPath) { $diskArguments += " --windows-reclaim-path `"$reclaimPath`"" }
}
$diskAction = New-ScheduledTaskAction -Execute $VenvPythonw -Argument $diskArguments
$diskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$diskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $DiskTaskName -Action $diskAction -Trigger $diskTrigger -Principal $trayPrincipal -Settings $diskSettings -Description "QuotaPool disk pressure monitor and guarded Windows/WSL cache maintenance" -Force | Out-Null
Start-ScheduledTask -TaskName $DiskTaskName

if ($WindowsWorktreeRepo.Count -eq 0 -or $WindowsWorktreeRoot.Count -eq 0) {
    foreach ($drive in $fixedDrives) {
        $driveRoot = $drive.DeviceID + "\"
        $evoxRepo = Join-Path $driveRoot "EvoMap_projects\evox"
        $desktopRepo = Join-Path $driveRoot "EvoMap_projects\evox-desktop"
        if (Test-Path -LiteralPath (Join-Path $evoxRepo ".git")) {
            if ($WindowsWorktreeRepo -notcontains $evoxRepo) { $WindowsWorktreeRepo += $evoxRepo }
            foreach ($root in @(
                (Join-Path $driveRoot "EvoMap_projects\evox-worktrees"),
                (Join-Path $evoxRepo ".claude\worktrees")
            )) {
                if ((Test-Path -LiteralPath $root -PathType Container) -and $WindowsWorktreeRoot -notcontains $root) {
                    $WindowsWorktreeRoot += $root
                }
            }
        }
        if (Test-Path -LiteralPath (Join-Path $desktopRepo ".git")) {
            if ($WindowsWorktreeRepo -notcontains $desktopRepo) { $WindowsWorktreeRepo += $desktopRepo }
            $root = Join-Path $desktopRepo ".claude\worktrees"
            if ((Test-Path -LiteralPath $root -PathType Container) -and $WindowsWorktreeRoot -notcontains $root) {
                $WindowsWorktreeRoot += $root
            }
        }
    }
}
if ($WindowsWorktreeRepo.Count -gt 0 -and $WindowsWorktreeRoot.Count -gt 0) {
    $worktreeState = Join-Path $StateDir "worktree-maintenance.json"
    $worktreeArguments = "-m claude_fleet.worktree_guard --state `"$worktreeState`" --max-age-days $WorktreeMaxAgeDays"
    foreach ($worktreeRepo in $WindowsWorktreeRepo) {
        if ($worktreeRepo) { $worktreeArguments += " --repo `"$worktreeRepo`"" }
    }
    foreach ($worktreeRoot in $WindowsWorktreeRoot) {
        if ($worktreeRoot) { $worktreeArguments += " --root `"$worktreeRoot`"" }
    }
    $worktreeAction = New-ScheduledTaskAction -Execute $VenvPythonw -Argument $worktreeArguments
    $worktreeTrigger = New-ScheduledTaskTrigger -Daily -At "03:30"
    $worktreeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $WorktreeTaskName -Action $worktreeAction -Trigger $worktreeTrigger -Principal $trayPrincipal -Settings $worktreeSettings -Description "QuotaPool guarded cleanup for expired clean Git worktrees; branches are preserved" -Force | Out-Null
}
Write-Host "Installed Quotapool Desktop adapter and started its tray monitor."
Write-Host "Installed local service watchdog and guarded disk maintenance (2-minute interval)."
Write-Host "Installed guarded worktree maintenance (daily at 03:30; branches and active work are protected)."
Write-Host "To inspect without changing anything: $Venv\Scripts\quotapool-desktop.exe inspect"
