# Quotapool on Windows and Claude Desktop

Quotapool's Windows adapter keeps the coordinator, TeamClaude data plane,
dashboard, and tray monitor local to the current Windows account. It does not
patch Claude Desktop, install a certificate, or expose the control plane to the
LAN. OAuth credentials remain in the local configuration and are never sent to
the dashboard browser.

Windows uses `127.0.0.1:13457` for its data plane and
`127.0.0.1:17893` for its coordinator. These intentionally differ from the
WSL defaults (`13456` and `17891`), because WSL mirrored networking can make a
Linux listener appear on Windows loopback and otherwise fool the Windows
watchdog into treating the WSL service as its own.

## Requirements

- Windows 11 with Python 3.9+ and Node.js 20+
- Microsoft Store Claude Desktop with its managed Claude Code binary
- PowerShell 7 or Windows PowerShell 5.1
- WSL 2 only when Claude Desktop should run projects stored in Linux

Preview installation without changing the machine:

```powershell
Set-Location C:\Users\12973\Projects\quotapool
.\install-windows.ps1 -DryRun
```

Install for the current Windows account:

```powershell
.\install-windows.ps1 -Install
```

The installer creates a private virtual environment under
`%LOCALAPPDATA%\Quotapool`, writes runtime state under
`%APPDATA%\Quotapool`, registers the `QuotaPool Tray Monitor` task, and points
Claude Desktop's `processWrapper` at Quotapool's wrapper. It preserves existing
OAuth accounts and does not modify the WindowsApps package.

It also registers `QuotaPool Service Watchdog`, which checks the Windows-local
coordinator and data plane every minute and restarts either process if needed.
`QuotaPool Disk Maintenance` checks the Windows host volume every two minutes.
When space is low it removes unlocked temporary files older than seven days
from both the current user's temp directory and `C:\Windows\Temp`. It never
follows junctions and leaves open files untouched.
The default policy starts safe maintenance and allows configured Rust
build-cache reclamation below 128 GiB free. Override those thresholds with
`QUOTAPOOL_DISK_MAINTENANCE_GB` and
`QUOTAPOOL_DISK_RECLAIM_GB`.

Inspect the integration without changing it:

```powershell
& "$env:LOCALAPPDATA\Quotapool\venv\Scripts\quotapool-desktop.exe" inspect
```

## Windows to WSL connection

Claude Desktop runs on Windows. A project may live and execute in WSL by using
an SSH connection. The local configuration uses:

| Setting | Value |
| --- | --- |
| Host | `kachi@127.0.0.1` |
| Port | `2222` |
| Identity file | `%USERPROFILE%\.ssh\quotapool-wsl-desktop` |
| Distribution | `Ubuntu-22.04` |

Verify the complete path from PowerShell before starting a long-running task:

```powershell
ssh -i "$env:USERPROFILE\.ssh\quotapool-wsl-desktop" -p 2222 kachi@127.0.0.1
```

`ECONNREFUSED` means Windows could not reach the listener, normally because WSL
stopped or `ssh.service` is not active. `Permission denied` means the TCP path
works and the identity/authorized key should be checked instead.

Keep the WSL distribution alive while Desktop tasks run. The local installation
uses the `Claude WSL SSH Keepalive` scheduled task and a once-per-minute
`Claude WSL SSH Watchdog` task so an unexpected exit is automatically repaired.
Neither helper starts or repairs WSL when the Windows host volume has less than
16 GiB free. This prevents low disk space from escalating into a wedged WSL/HCS
virtual machine. Set `QUOTAPOOL_WSL_MIN_FREE_GB` to override the threshold.
The watchdog completes a public-key login through the configured SSH identity,
instead of trusting a TCP port or SSH banner that can remain visible while WSL
is wedged. It restarts only `ssh.service`
when WSL is still running but the SSH listener has failed; it never terminates
the WSL distribution or a remote Claude task. Both tasks run through
`pythonw.exe`, and every `wsl.exe`/`schtasks.exe` child is created with
`CREATE_NO_WINDOW`, so the one-minute status check does not flash a Command
Prompt or PowerShell window. Task result `2` means the listener did not return
an SSH banner but an established Claude connection is still active; the
watchdog deliberately preserves that session and repairs SSH after it exits.
Task result `4` means the Windows host volume is below the free-space guard.

The Windows installation also registers `QuotaPool WSL Trim` at 03:30 daily.
It runs `fstrim` invisibly so deleted Linux files release sparse VHD blocks back
to Windows. `StartWhenAvailable` catches up after a missed run, while the same
free-space guard prevents the maintenance task from booting a stopped WSL VM on
an already-critical host volume.

The 10-minute disk guard follows a stricter safety order:

1. Delete only files older than seven days in the current user's Windows temp
   directory, skipping locked files and filesystem reparse points.
2. Bound WSL journal retention, clear the APT download cache, and run `fstrim`.
3. Below 128 GiB, reclaim only configured Rust `target` directories. This last
   step is skipped unless there are zero established Claude SSH connections,
   no `cargo`, `rustc`, or `sccache` process, and no process has its working
   directory or executable inside the target directory.

Source trees, models, Docker data, agent sessions, transcripts, credentials,
and arbitrary user directories are never maintenance targets. The latest result
is stored in `%LOCALAPPDATA%\Quotapool\state\disk-maintenance.json` and shown in
the dashboard. Task result `4` means the volume remains below the 16 GiB hard
safety floor after all permitted maintenance.

Useful read-only checks:

```powershell
wsl.exe --list --verbose
Test-NetConnection 127.0.0.1 -Port 2222
Get-ScheduledTask -TaskName 'Claude WSL SSH Keepalive','Claude WSL SSH Watchdog'
```

## Local dashboard

The Windows adapter includes a loopback-only dashboard. It never sends OAuth
tokens to the browser and binds only to `127.0.0.1`:

```powershell
& "$env:LOCALAPPDATA\Quotapool\venv\Scripts\quotapool-desktop.exe" dashboard --open
```

Then open <http://127.0.0.1:17892/>. The dashboard shows service health,
account status, 5-hour and 7-day quota bars, current routing, active sessions,
and model routes. On Windows it also displays a WSL connection panel with:

- distribution and SSH listener state;
- TCP reachability and the exact SSH command;
- WSL and Windows disk usage;
- keepalive, watchdog, and tray task state;
- disk-guard state, last run time, and reclaimed space;
- a short Windows-to-WSL troubleshooting guide.

The tray icon opens this dashboard and reports service/account health without
opening a console window. Dashboard and tray process probes use
`CREATE_NO_WINDOW` as well.

## Connection lifecycle and automatic state maintenance

The data plane bounds client sockets and admitted model requests, removes a
request from the live registry when either side closes, and exposes the result
in the Resource Management page. The coordinator performs a conservative
state sweep after startup and every six hours; cleanup failures are reported
but never take request routing down.

Operators can tune the schedule without editing code:

- `FLEET_MAINTENANCE_INITIAL_DELAY_SECONDS` (default `60`)
- `FLEET_MAINTENANCE_INTERVAL_SECONDS` (default `21600`)

Windows service recovery fails closed: an unhealthy port is terminated only
after its PID, executable and full QuotaPool command are verified, and the
same PID is observed unhealthy twice at least 30 seconds apart. The recovery
observation is stored in
`%LOCALAPPDATA%\Quotapool\state\service-recovery.json`; healthy services clear
their own observation. The service PID ledger always records the real listener
PID rather than a short-lived virtual-environment or NVM launcher.

## Expired worktree maintenance

The Windows installer registers `QuotaPool Worktree Maintenance` for 03:30
daily. It manages only explicitly detected EvoX and EvoX Desktop temporary
roots. Codex-owned `%USERPROFILE%\.codex\worktrees`, standalone worktrees, the
main checkout, and every path outside those roots are excluded.

A worktree is eligible only after 14 days without Git, file, or Claude
transcript activity, and only when all of these checks pass:

- no process command line references the worktree;
- `git status --porcelain` is empty;
- the worktree is not locked or a filesystem reparse point;
- its HEAD is reachable from another local or remote Git ref.

Cleanup uses `git worktree remove` without `--force`. The branch is never
deleted, so the worktree remains reconstructable. If process inspection fails,
the whole pass fails closed. The latest inventory and decision for every
worktree is written to
`%LOCALAPPDATA%\Quotapool\state\worktree-maintenance.json` and summarized in
the Resource Management dashboard.

To preview the current policy without removing anything:

```powershell
& "$env:LOCALAPPDATA\Quotapool\venv\Scripts\python.exe" -m claude_fleet.worktree_guard `
  --dry-run --state "$env:TEMP\quotapool-worktree-preview.json" `
  --repo "E:\EvoMap_projects\evox" --root "E:\EvoMap_projects\evox-worktrees"
```

## Troubleshooting

If the dashboard does not load, check the private services:

```powershell
& "$env:LOCALAPPDATA\Quotapool\venv\Scripts\quotapool-desktop.exe" status
```

If Claude Desktop does not use Quotapool, inspect
`%USERPROFILE%\.claude\settings.json` and confirm `processWrapper` points to
the installed Quotapool wrapper. Use the dashboard's Desktop action to open or
restart the Store app; it targets only Claude Desktop processes and does not
terminate active Claude Code or WSL tasks.

If a command window still flashes after the silent tasks are installed, record
the approximate time. Compare it with Task Scheduler's last-run times; build
tools such as `cmd.exe /c tsc` or Vite terminals are separate from Quotapool's
status checks and should be traced to their own parent process.
