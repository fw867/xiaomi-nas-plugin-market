<#
    mount-usb-ext4.ps1
    Attach a USB-attached ext4 disk to WSL, then open it in Windows Explorer.

    Usage (must be elevated):
      Right-click this file -> "Run with PowerShell"   (accept the UAC prompt)
      or, from an already-elevated PowerShell:
          .\mount-usb-ext4.ps1
          .\mount-usb-ext4.ps1 -DiskNumber 4 -Partition 1
          .\mount-usb-ext4.ps1 -DiskNumber 0          # auto-detect USB Linux disk

    What it does:
      * wsl --mount needs Administrator, and the disk must not be in use by Windows
      * the ext4 partition appears inside WSL at  /mnt/wsl/PHYSICALDRIVE<n>p<part>
      * Windows reads it through  \\wsl.localhost\Ubuntu\mnt\wsl\PHYSICALDRIVE<n>p<part>
      * to detach again:  wsl --unmount \\.\PHYSICALDRIVE<n>

    Note: the attachment does not survive "wsl --shutdown" or a Windows reboot,
    so re-run this script after either.
#>
[CmdletBinding()]
param(
    [int]$DiskNumber = 4,      # 0 = auto-detect the USB disk holding a Linux partition
    [int]$Partition  = 1,
    [switch]$NoExplorer
)

$ErrorActionPreference = 'Stop'

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Administrator rights are required. Right-click this file and choose 'Run with PowerShell'." -ForegroundColor Red
    exit 1
}

if ($DiskNumber -eq 0) {
    $target = $null
    foreach ($candidate in (Get-Disk | Where-Object BusType -eq 'USB')) {
        $parts = Get-Partition -DiskNumber $candidate.Number -ErrorAction SilentlyContinue
        if ($parts | Where-Object { $_.Type -in @('Linux filesystem', 'Unknown') }) {
            $target = $candidate
            break
        }
    }
    if (-not $target) {
        Write-Host "No USB disk with a Linux partition was found." -ForegroundColor Red
        exit 1
    }
    $DiskNumber = $target.Number
    Write-Host ("Auto-selected: disk {0}  {1}  {2:N1} GB" -f $target.Number, $target.FriendlyName, ($target.Size / 1GB))
}

$diskPath = "\\.\PHYSICALDRIVE$DiskNumber"
$uncPath  = "\\wsl.localhost\Ubuntu\mnt\wsl\PHYSICALDRIVE${DiskNumber}p${Partition}"

# Make sure the distro is up before attaching
& wsl --distribution Ubuntu --exec /bin/true 2>&1 | Out-Null

Write-Host "Attaching $diskPath partition $Partition to WSL ..."

$output = & wsl --mount $diskPath --partition $Partition 2>&1
$mountExit = $LASTEXITCODE
# wsl.exe writes UTF-16LE here, so the captured text can carry NUL bytes
# between characters: match loosely instead of against a literal.
$outputText = ($output | Out-String) -replace "`0", ''
$alreadyMounted = $outputText -match 'ALREADY_MOUNTED'

if ($alreadyMounted) {
    Write-Host "This disk is already attached to WSL; verifying it instead." -ForegroundColor Yellow
} else {
    $output | ForEach-Object { Write-Host $_ }
}

$attached = $false
if (Test-Path -LiteralPath $uncPath) {
    # A stale, still-empty mountpoint survives a WSL restart: the folder exists
    # but nothing is mounted on it. Only real content proves the attach worked.
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        $entries = @(Get-ChildItem -LiteralPath $uncPath -Force -ErrorAction SilentlyContinue)
        if ($entries.Count -gt 0) { $attached = $true; break }
        Start-Sleep -Milliseconds 800
    }
}

if (-not $attached) {
    Write-Host ""
    Write-Host "The mount did NOT take effect: $uncPath is still empty." -ForegroundColor Red
    if (-not $alreadyMounted) { Write-Host "wsl --mount exit code: $mountExit" }
    Write-Host "Typical causes:"
    Write-Host "  * the disk is still in use by Windows (close Explorer windows / programs on it)"
    Write-Host "  * wrong -DiskNumber / -Partition"
    Write-Host "  * the disk was left half-attached; run:  wsl --unmount \\.\PHYSICALDRIVE$DiskNumber"
    Write-Host "    then run this script again."
    exit 1
}

Write-Host ""
Write-Host "Mounted and readable from Windows at:" -ForegroundColor Green
Write-Host "  $uncPath"
$names = @()
Get-ChildItem -LiteralPath $uncPath -Force | ForEach-Object { $names += $_.Name }
Write-Host ("Top-level entries: " + ($names -join ', '))
Write-Host "To detach later:  wsl --unmount \\.\PHYSICALDRIVE$DiskNumber"

if (-not $NoExplorer) {
    Start-Process explorer.exe -ArgumentList $uncPath
}

exit 0
