# NTFS structures used by the recovery tool

Everything here is what `scripts/ntfs_recover.py` reads. Values are little-endian.

## Boot sector (offset 0 of the partition)

| Offset | Size | Field |
|---|---|---|
| 0x03 | 8 | OEM ID (`NTFS    `) |
| 0x0B | 2 | bytes per sector |
| 0x0D | 1 | sectors per cluster |
| 0x28 | 8 | total sectors |
| 0x30 | 8 | `$MFT` starting cluster (LCN) |
| 0x38 | 8 | `$MFTMirr` starting LCN |
| 0x40 | 1 | clusters per MFT record (signed): positive = count, negative = 2^-n bytes |

## MFT record (1024 bytes typical)

| Offset | Size | Field |
|---|---|---|
| 0x00 | 4 | magic `FILE` |
| 0x04 | 2 | update sequence array offset |
| 0x06 | 2 | update sequence array count |
| 0x10 | 2 | sequence number |
| 0x12 | 2 | hard link count |
| 0x14 | 2 | offset to first attribute |
| 0x16 | 2 | flags: bit0 = in use, bit1 = directory |
| 0x20 | 8 | base record reference (non-zero = this record extends another) |
| 0x2C | 4 | MFT record number |

Attribute headers follow the first-attribute offset.

| Resident | Non-resident |
|---|---|
| 0x00 type (4) | 0x00 type (4) |
| 0x04 length (4) | 0x04 length (4) |
| 0x08 non-resident flag (0) | 0x08 non-resident flag (1) |
| 0x09 name length | 0x09 name length |
| 0x0A name offset | 0x0A name offset |
| 0x0C flags | 0x0C flags (bit0 = compressed) |
| 0x0E attribute id | 0x0E attribute id |
| 0x10 value length (4) | 0x10 lowest VCN (8) |
| 0x14 value offset (2) | 0x18 highest VCN (8) |
| 0x16 value flags (1) | 0x20 run list offset (2) |
| | 0x28 allocated size (8) |
| | 0x30 real size (8) |
| | 0x38 initialized size (8) |

Attribute types: `0x10` `$STANDARD_INFORMATION`, `0x20` `$ATTRIBUTE_LIST`,
`0x30` `$FILE_NAME`, `0x80` `$DATA`.

## `$FILE_NAME` value

| Offset | Size | Field |
|---|---|---|
| 0x00 | 8 | parent file reference (FRN) |
| 0x08 | 8 | creation time (FILETIME) |
| 0x10 | 8 | modification time |
| 0x18 | 8 | MFT change time |
| 0x20 | 8 | access time |
| 0x28 | 8 | allocated size |
| 0x30 | 8 | real data size |
| 0x38 | 4 | flags |
| 0x3C | 4 | reparse tag |
| 0x40 | 1 | file name length (chars) |
| 0x41 | 1 | namespace: 0 = POSIX, 1 = Win32, 2 = DOS 8.3, 3 = Win32 + POSIX |
| 0x42 | var | name, UTF-16LE |

**Namespace 2 is the 8.3 short name.** A long file usually has two
`$FILE_NAME` attributes: one namespace 3 (long) and one namespace 2 (short).
Pick namespace 3/1 first when rebuilding paths, or every path comes out as
`PROGRA~2` and `DOKUME~1`.

## File reference numbers (FRN)

A reference is **48 bits of MFT record number + 16 bits of sequence number**:

```
record_index = frn & 0xFFFFFFFFFFFF
sequence     = frn >> 48
```

Comparing an unmasked FRN against a record index silently fails, and every
parent lookup then resolves to nothing — all files collapse into one bucket
named after the volume root. This is the single most common bug in hand-rolled
MFT readers.

## Run list encoding

A run list is a sequence of header bytes, each followed by a length field and
optionally a signed LCN delta:

```
header byte:  bits 0-3 = length field size
              bits 4-7 = offset field size
length:       little-endian unsigned, in clusters
offset:       little-endian SIGNED, delta from the previous LCN
```

An offset field size of 0 means the run is a **sparse hole** (read as zeros).
A terminating `0x00` header ends the list.

Total bytes = sum(run_length) x cluster_size.

**Recycled records** keep a stale run list with absurd lengths. Validate before
copying: run length <= 2^26 clusters, LCN inside the volume, and total bytes
consistent with the recorded real size. An unvalidated run list produces a
multi-terabyte file that fills the destination and buries the real data.

## Deleted-record semantics

- The record header's in-use bit is cleared, but the attributes remain.
- The record is only overwritten when the MFT slot is **reused**. A record
  whose base record reference is non-zero is an extension of another record,
  not an independent file.
- Once reused, the original name and run list are gone. That is the boundary
  between recoverable and not, and it is why recovery should start before the
  volume fills up with new data.

## Reconstructing a path

Follow parent FRNs from the file's `$FILE_NAME` attribute up to the volume
root, collecting names. A parent record that was deleted and its slot reused
breaks the chain: the file still exists but its path can no longer be derived.
Both the USN Journal and the MFT can supply the chain; the USN Journal records
the traversal **through reparse points**, so a junction shows up as an extra
path segment that the MFT parent references do not contain. Both views are
correct and they are how you detect that a junction was involved.

## Volume layout

| Offset | Size | Field |
|---|---|---|
| 0x00 | 3 | jump instruction |
| 0x03 | 8 | OEM ID |
| 0x0B | 2 | bytes per sector |
| 0x0D | 1 | sectors per cluster |
| 0x28 | 8 | total sectors |
| 0x30 | 8 | `$MFT` LCN |
| 0x38 | 8 | `$MFTMirr` LCN |
| 0x40 | 1 | clusters per MFT record (signed) |
| 0x50 | 8 | volume serial number |
| 0x68 | 8 | MFT zone / reserved |

`$MFT` itself is usually a non-resident file with its own run list; read the
run list from MFT record 0 instead of assuming a contiguous layout.

## Reading raw

Open `\\.\PhysicalDriveN` with `GENERIC_READ` and `FILE_SHARE_READ|FILE_SHARE_WRITE`.
Reads must be **sector aligned** — a misaligned `ReadFile` on a raw device fails
with `ERROR_INVALID_PARAMETER` (87). Set the file pointer with `SetFilePointerEx`
and read with `ReadFile`; `os.pread` does not exist on Windows.
