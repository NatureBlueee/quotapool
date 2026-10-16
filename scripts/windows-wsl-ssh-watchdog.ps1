[CmdletBinding()]
param(
    [string]$Distribution = "Ubuntu-22.04",
    [int]$Port = 2222,
    [string]$KeepaliveTask = "Claude WSL SSH Keepalive"
)

$ErrorActionPreference = "Stop"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"

function Test-LocalTcp([int]$TargetPort, [int]$TimeoutMilliseconds = 800) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync("127.0.0.1", $TargetPort)
        return $pending.Wait($TimeoutMilliseconds) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-LocalTcp([int]$TargetPort, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (Test-LocalTcp $TargetPort) { return $true }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)
    return $false
}

$keepalive = Get-ScheduledTask -TaskName $KeepaliveTask -ErrorAction SilentlyContinue
if ($null -eq $keepalive) {
    throw "Scheduled task '$KeepaliveTask' is missing."
}
if ($keepalive.State -ne "Running") {
    Start-ScheduledTask -TaskName $KeepaliveTask
}

# A stopped distribution may need a few seconds after the keepalive task starts.
if (Wait-LocalTcp $Port 6) { exit 0 }

# WSL can remain Running while ssh.service has failed. Repair only that service;
# do not terminate the distribution, remote Claude server, or user tasks.
& $wsl -d $Distribution -u root --exec /usr/bin/systemctl restart ssh
if ($LASTEXITCODE -ne 0) {
    throw "Could not restart ssh.service in $Distribution."
}
if (-not (Wait-LocalTcp $Port 6)) {
    throw "SSH did not become reachable on 127.0.0.1:$Port."
}
