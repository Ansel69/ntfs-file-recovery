---
name: ntfs-file-recovery
description: >-
  Recover files deleted from a Windows NTFS drive when no backup, Shadow Copy, or
  Recycle Bin entry survives, and diagnose what deleted them. Use when files
  vanish from a drive after a mass deletion, an agent or script ran a recursive
  delete, a suspected junction/loop caused a tree to be wiped, or the user asks
  "how do I get my files back" / "what deleted my files". Covers NTFS $MFT
  parsing with no third-party tools, USN Journal forensics, PowerShell event-log
  root-causing, and junction/symlink footguns in `rd /s /q` and `Remove-Item -Recurse`.
---

# NTFS File Recovery

Two jobs that belong together: **prove what happened**, then **get the files back**.
Do them in that order — knowing the deletion mechanism tells you which data is
still intact and whether the source volume is safe to write to.

## Golden rules

0. **Run elevated.** Raw disk access (`\\.\PhysicalDriveN`) needs an
   Administrator shell; a medium-integrity shell fails with
   `ERROR_ACCESS_DENIED` (5). In PowerShell:
   `Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile","-Command","python scripts\ntfs_recover.py ..."`.
   Read-only inspection of the volume (PowerShell, `fsutil`, event logs) does
   not require elevation.
1. **Never write to the source volume.** Every byte you write can overwrite the
   clusters holding files you have not recovered yet. Recover to a *different*
   volume. If only one volume exists, recover to a network share or an external
   disk, and stop writing to the source entirely until you are done.
2. **Diagnose before recovering.** The deletion mechanism determines whether the
   MFT records still point at live data or have already been recycled.
3. **Read the MFT before you trust a tool.** `$MFT` holds the filename, the
   parent reference, the size and the data-run list for every file, deleted or
   not, until those records get reused. That is often weeks of data with no
   third-party tool and no backup.

## Phase 1 — Triage (read-only)

Run `scripts/triage.ps1 -Drive E`. It reports, without modifying anything:

- USN Journal window and the deleted-file timeline (alphabetical walk order
  across top-level folders is the signature of a recursive delete)
- PowerShell `Windows PowerShell` log entries whose `HostApplication` contains a
  destructive command, plus `Microsoft-Windows-PowerShell/Operational` 4104
  script-block bodies if script-block logging is on
- Shadow Copies / File History / Windows Backup availability
- Recycle Bin integrity (`$I`/`$R` pair validity) and orphaned `$I`/`$R` files
- Directory junctions and reparse points on the volume — see the footgun below

Read the timeline top-to-bottom before touching anything. The gap between the
last clean command and the first delete usually names the culprit exactly.

## Phase 2 — Recover from the MFT

Run `scripts/ntfs_recover.py`:

```
python ntfs_recover.py --drive E --list                 # inventory of deleted files
python ntfs_recover.py --drive E --out F:\recovered --by-size-desc
python ntfs_recover.py --drive E --out F:\recovered --match "\.pdf$" --match "DOKUMENTASI"
```

It parses the boot sector, locates `$MFT` (including multi-run `$MFT`),
reconstructs each deleted file's path from parent file references, and copies the
data out of the recorded clusters. Output keeps the original directory tree.

Critical details it already handles (do not re-derive these):

- **FRN masking.** A file reference is 48 bits of record number plus 16 bits of
  sequence number. Compare `frn & 0xFFFFFFFFFFFF` against the record index, or
  every parent lookup silently fails and all paths collapse to one bucket.
- **Long vs 8.3 names.** A directory can carry both a short name and a long
  name in separate `$FILE_NAME` attributes. Prefer the long name (namespace 1 or
  3) when rebuilding paths, or you get `PROGRA~2` instead of `Program Files (x86)`.
- **Size is not the truth.** Trust the run list for byte count. `$FILE_NAME`
  sizes can be stale or zero for deleted records; a run list claiming 2^60
  clusters means the record was recycled and the file is unrecoverable.
- **Validate every run list** (run length within sane bounds, LCN inside the
  volume, total bytes consistent with the recorded size). A corrupt run list
  produces a gigantic file that fills the destination and hides real data.

Order extraction by **starting LCN, not by name**. On spinning or SMR drives
that turns thousands of random seeks into a near-sequential pass; the difference
measured here was 3 MB/s versus 140 MB/s on the same data.

### Verify what came out

Check magic bytes before trusting a recovered file: `%PDF` for PDF, `\xFF\xD8\xFF`
for JPEG, `\x89PNG` for PNG, `SFPK`/`SBPK` for sPackage, `ftyp` for MP4/MOV,
`PE\0\0` for DLL/EXE. Sample a few hundred files and report the hit rate. Flag
the rest rather than claiming success.

## The junction footgun (this causes whole-drive wipes)

`cmd.exe`'s `rd /s /q` and PowerShell's `Remove-Item -Recurse` behave
differently on **directory junctions and symlinks**:

- `rd /s /q <junction>` walks **through** the junction and deletes the
  *target's* contents. Point it at a junction to a drive root and it wipes the
  root.
- `Remove-Item -Recurse` on a junction removes the link, not the target.

Evidence that a junction caused a wipe: deleted paths are reported under the
junction path in the USN Journal, while the `$MFT` parent references point at
the real location. Both views are correct — the junction is a reparse point and
NTFS records the traversal through it.

**Always enumerate reparse points before running any recursive delete**, and
resolve them to their real targets before quoting a path in a command. If a
target path looks like a drive root, stop.

## Restore-in-place ordering

If the recovered files must go back to the volume they came from, extract
everything to another volume first, then copy back. Writing to the source while
still reading clusters from it lets the allocator hand you back clusters that
hold not-yet-recovered files.

## Reference

`reference/ntfs-layout.md` — boot sector fields, MFT record layout, attribute
headers, run-list encoding, and the FRN/sequence-number rule.

## Real-world impact

A 742 GB restore on a drive whose entire contents had been wiped was recovered
at 99.1% file success from `$MFT` alone — no backup, no Shadow Copy, no
commercial tool — because the MFT records had not yet been recycled. The
remaining 0.9% was game-engine binaries and archive files that had been
re-downloaded since the deletion and were therefore genuinely overwritten.
