<#
.SYNOPSIS
  Read-only triage for "my files disappeared" on a Windows drive.

.DESCRIPTION
  Collects evidence without modifying the volume:
    * USN Journal window + deleted-file timeline (alphabetical walk = recursive delete)
    * PowerShell Windows PowerShell / Operational logs with destructive commands
    * Backup availability: Shadow Copies, Windows Backup, File History, Restore Points
    * Recycle Bin integrity ($I/$R pairing) and orphaned $I/$R files
    * Directory junctions / reparse points (the rd /s /q footgun)

.PARAMETER Drive
  Drive letter to inspect, e.g. E

.EXAMPLE
  .\triage.ps1 -Drive E
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Drive)

$ErrorActionPreference = 'SilentlyContinue'
$Drive = $Drive.TrimEnd(':\').ToUpper()
$root = "${Drive}:\"

function Section($t) { Write-Output ""; Write-Output ("=" * 78); Write-Output "  $t"; Write-Output ("=" * 78) }

Section "1. VOLUME"
Get-Volume -DriveLetter $Drive | Select-Object DriveLetter, FileSystem,
    @{n = 'SizeGB'; e = { [math]::Round($_.Size / 1GB, 1) } },
    @{n = 'FreeGB'; e = { [math]::Round($_.SizeRemaining / 1GB, 1) } },
    HealthStatus, OperationalStatus | Format-List
Get-Partition -DriveLetter $Drive | Select-Object DiskNumber, PartitionNumber,
    @{n = 'OffsetMB'; e = { [math]::Round($_.Offset / 1MB, 1) } } | Format-List

Section "2. USN JOURNAL (deleted-file timeline)"
$j = fsutil usn queryjournal $Drive 2>&1
$j | Select-Object -First 12
$tmp = Join-Path $env:TEMP ("usn_{0}.csv" -f $Drive)
Write-Output "  dumping journal to $tmp ..."
fsutil usn readjournal $Drive csv > $tmp 2>$null
$rows = Select-String -Path $tmp -Pattern '"File delete' -SimpleMatch
Write-Output ("  'File delete' records: {0}" -f $rows.Count)
Write-Output "  deleted names containing 'nexus' (sample):"
$rows | Where-Object { $_.Line -match 'nexus' } | Select-Object -First 5 |
    ForEach-Object { ($_.Line -split ',')[1] }

Section "3. POINTER SHIFTS (no Volume Shadow Copy = no backup)"
$sc = vssadmin list shadows 2>&1
if ($sc -match 'No items found') { Write-Output "  NO Shadow Copies on this system." }
else { $sc | Select-Object -First 20 }
Write-Output "  Windows Backup:"
(wbadmin get versions -backupTarget:$Drive 2>&1 | Select-Object -First 6)
Write-Output "  System Restore points:"
(Get-ComputerRestorePoint | Select-Object -First 5 SequenceNumber, Description, CreationTime | Format-Table -AutoSize | Out-String)

Section "4. DESTRUCTIVE COMMANDS IN POWERSHELL LOG"
$ev = Get-WinEvent -FilterHashtable @{ LogName = 'Windows PowerShell' } -MaxEvents 800
$hits = $ev | Where-Object { $_.Message -match 'Remove-Item|rd /s|rmdir|rm -rf|del /|Format-|Clear-RecycleBin' }
if ($hits) {
    $hits | Select-Object -First 12 | ForEach-Object {
        Write-Output ("  [{0}] {1}" -f $_.TimeCreated, ($_.Message -replace '\s+', ' '))
    }
} else { Write-Output "  no destructive commands found in the last 800 events" }

Section "5. SCRIPT BLOCK LOGGING (4104) - exact commands"
$sb = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-PowerShell/Operational'; Id = 4104 } -MaxEvents 200
if ($sb) {
    $sb | Where-Object { $_.Message -match 'Remove-Item|rmdir|rd /s' } | Select-Object -First 5 |
        ForEach-Object { Write-Output ("  [{0}] {1}" -f $_.TimeCreated, (($_.Message -replace '\s+', ' ').Substring(0, [Math]::Min(600, $_.Message.Length)))) }
} else { Write-Output "  script block logging not enabled" }

Section "6. RECYCLE BIN INTEGRITY"
$bin = "${root}`$RECYCLE.BIN"
if (Test-Path -LiteralPath $bin) {
    Get-ChildItem -LiteralPath $bin -Force -Recurse |
        Where-Object { $_.Name -match '^\$[IR]' } |
        ForEach-Object { Write-Output ("  {0}  {1} bytes" -f $_.FullName, $_.Length) }
    $orphans = Get-ChildItem -LiteralPath $root -Force -File |
        Where-Object { $_.Name -match '^\$[IR]' }
    if ($orphans) {
        Write-Output "  ORPHANED recycle-bin entries sitting in the volume root (these corrupt the bin):"
        $orphans | ForEach-Object { Write-Output ("    {0}  {1} bytes" -f $_.Name, $_.Length) }
    } else { Write-Output "  no orphaned `$I/`$R in the volume root" }
} else { Write-Output "  no $RECYCLE.BIN on this volume" }

Section "7. JUNCTIONS / REPARSE POINTS (rd /s /q follows these)"
$rp = Get-ChildItem -LiteralPath $root -Force -Recurse -Depth 3 |
    Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
if ($rp) {
    $rp | Select-Object -First 40 | ForEach-Object {
        $t = $_.Target
        if (-not $t) { $t = (cmd /c "fsutil reparsepoint query `"$($_.FullName)`"" 2>&1 | Select-String 'Print Name' | Select-Object -First 1) }
        Write-Output ("  {0}  ->  {1}" -f $_.FullName, $t)
    }
    Write-Output "  WARNING: a junction pointing at a drive root turns 'rd /s /q' into a full-volume wipe."
} else { Write-Output "  no reparse points found in the top 3 levels" }

Section "8. SUMMARY"
Write-Output "  Deleted file records (USN)   : see section 2"
Write-Output "  Shadow Copy / Windows Backup  : see section 3"
Write-Output "  Suspect command               : see sections 4-5"
Write-Output "  Next step: run ntfs_recover.py --drive $Drive --list"
Write-Output "  Recover to a DIFFERENT volume. Never write to $Drive while recovering."
