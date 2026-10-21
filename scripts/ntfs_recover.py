#!/usr/bin/env python3
"""
ntfs_recover.py - Recover deleted files from a Windows NTFS volume by parsing $MFT.

No third-party dependencies. Read-only against the source volume.

Usage:
    python ntfs_recover.py --drive E --list
    python ntfs_recover.py --drive E --out F:\\recovered
    python ntfs_recover.py --drive E --out F:\\recovered --match "\\.pdf$" --match "DOKUMENTASI"
    python ntfs_recover.py --drive E --out F:\\recovered --by-size-desc --verify

Key implementation notes (see reference/ntfs-layout.md):
  * File references are 48-bit record number + 16-bit sequence number.
    Always mask: frn & 0xFFFFFFFFFFFF.
  * Prefer the long name ($FILE_NAME namespace 1 or 3) over the 8.3 short name.
  * Derive byte count from the run list; $FILE_NAME sizes can be stale or zero.
  * Validate run lists before trusting them; a recycled record yields absurd
    run lengths that would fill the destination with garbage.
  * Extract in ascending LCN order so the disk is read near-sequentially.
"""
import argparse
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import time

MASK = (1 << 48) - 1
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
CLUSTER = 4096
MAX_RUN_CLUSTERS = 1 << 26          # 64 GiB per run
MAX_TOTAL_BYTES = 1 << 40            # 1 TiB per file
MAX_SECTOR = 1 << 28                 # 1 TiB

MAGIC = {
    ".pdf": [b"%PDF"], ".jpg": [b"\xff\xd8\xff"], ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG"], ".gif": [b"GIF8"], ".bmp": [b"BM"],
    ".zip": [b"PK\x03\x04"], ".docx": [b"PK\x03\x04"], ".xlsx": [b"PK\x03\x04"],
    ".pptx": [b"PK\x03\x04"], ".7z": [b"7z\xbc\xaf"], ".rar": [b"Rar!"],
    ".psd": [b"8BPS"], ".mkv": [b"\x1a\x45\xdf\xa3"], ".webp": [b"RIFF"],
    ".heic": [b"ftyp"], ".avif": [b"ftyp"], ".mp4": [b"ftyp", b"moov", b"mdat", b"\x00\x00\x00"],
    ".mov": [b"ftyp", b"moov", b"mdat", b"\x00\x00\x00"],
    ".dll": [b"MZ"], ".exe": [b"MZ"], ".iso": [b"CD001"], ".cab": [b"MSCF"],
    ".unity3d": [b"UnityFS"], ".ab": [b"UnityFS", b"\x00\x00\x00"],
}

# ---------------------------------------------------------------- raw access
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]
k32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_longlong,
                                 ctypes.c_void_p, ctypes.c_uint32]
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]


class RawDisk:
    """Sector-aligned raw reads against a physical drive."""

    def __init__(self, device):
        self.h = k32.CreateFileW(device, 0x80000000, 1 | 2, None, 3, 0, None)
        if not self.h or self.h == ctypes.c_void_p(-1).value:
            raise OSError("cannot open %s (err %d)" % (device, ctypes.get_last_error()))

    def read(self, offset, size):
        pos = ctypes.c_longlong(offset)
        if not k32.SetFilePointerEx(self.h, pos, ctypes.byref(pos), 0):
            raise OSError("SetFilePointerEx failed")
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_uint32(0)
        if not k32.ReadFile(self.h, buf, size, ctypes.byref(got), None):
            raise OSError("ReadFile failed (%d)" % ctypes.get_last_error())
        return buf.raw[:got.value]

    def close(self):
        k32.CloseHandle(self.h)


# ------------------------------------------------------------------ geometry
def drive_to_device(letter):
    """Return (device_path, partition_offset_bytes) for a drive letter."""
    letter = letter.rstrip(":").upper()
    ps = ("$p=Get-Partition -DriveLetter %s;"
          "Write-Output ($p.DiskNumber); Write-Output $p.Offset" % letter)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True).stdout.split()
    if len(out) < 2:
        raise SystemExit("cannot resolve drive %s" % letter)
    return r"\\.\PhysicalDrive%s" % out[0], int(out[1])


def read_geometry(disk, part_off):
    boot = disk.read(part_off, 512)
    if boot[3:11] != b"NTFS    ":
        raise SystemExit("volume is not NTFS")
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    cluster = bps * spc
    total_sectors = struct.unpack_from("<Q", boot, 40)[0]
    mft_lcn = struct.unpack_from("<Q", boot, 48)[0]
    cpr = struct.unpack_from("<b", boot, 64)[0]
    rec_size = cluster * cpr if cpr > 0 else (1 << (-cpr))
    return dict(cluster=cluster, total_sectors=total_sectors,
                mft_lcn=mft_lcn, rec_size=rec_size, total_bytes=total_sectors * bps)


def parse_runlist(buf, off):
    runs, lcn = [], 0
    p = off
    while p < len(buf):
        h = buf[p]
        p += 1
        if h == 0:
            break
        ls, osz = h & 0x0F, (h >> 4) & 0x0F
        if ls == 0:
            break
        length = int.from_bytes(buf[p:p + ls], "little")
        p += ls
        if osz:
            delta = int.from_bytes(buf[p:p + osz], "little", signed=True)
            p += osz
            lcn += delta
            runs.append((lcn, length))
        else:
            runs.append((None, length))          # sparse hole
    return runs


def mft_map(disk, part_off, geo):
    """Byte map of $MFT: [(mft_relative_start, mft_relative_end, disk_offset)]."""
    rec0 = disk.read(part_off + geo["mft_lcn"] * geo["cluster"], geo["rec_size"])
    off = struct.unpack_from("<H", rec0, 20)[0]
    runs = size = None
    while off < geo["rec_size"] - 8:
        atype = struct.unpack_from("<I", rec0, off)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from("<I", rec0, off + 4)[0]
        if alen < 16:
            break
        if atype == ATTR_DATA and rec0[off + 8] == 1:
            size = struct.unpack_from("<Q", rec0, off + 48)[0]
            runs = parse_runlist(rec0, off + struct.unpack_from("<H", rec0, off + 32)[0])
        off += alen
    if not runs:
        raise SystemExit("cannot read $MFT run list")
    mapping, acc = [], 0
    for lcn, length in runs:
        nb = length * geo["cluster"]
        mapping.append((acc, acc + nb, part_off + lcn * geo["cluster"]))
        acc += nb
    geo["mft_size"] = size
    geo["mft_map"] = mapping
    geo["nrec"] = (size or 0) // geo["rec_size"]
    return geo


def read_mft(disk, geo, start, length):
    out = b""
    for s, e, dsk in geo["mft_map"]:
        a, b = max(s, start), min(e, start + length)
        if b > a:
            out += disk.read(dsk + (a - s), b - a)
    return out


# ------------------------------------------------------------------- records
def parse_record(rec, index):
    if len(rec) < 64 or rec[:4] != b"FILE":
        return None
    flags = struct.unpack_from("<H", rec, 22)[0]
    base_ref = struct.unpack_from("<Q", rec, 32)[0]
    if base_ref != 0:
        return None                                   # record reused as extension
    off = struct.unpack_from("<H", rec, 20)[0]
    names, data = [], None
    while off < len(rec) - 8:
        atype = struct.unpack_from("<I", rec, off)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from("<I", rec, off + 4)[0]
        if alen < 16 or off + alen > len(rec):
            break
        nonres = rec[off + 8]
        if atype == ATTR_FILE_NAME:
            if not nonres:
                vlen = struct.unpack_from("<I", rec, off + 16)[0]
                voff = struct.unpack_from("<H", rec, off + 20)[0]
                v = rec[off + voff:off + voff + min(vlen, 1024)]
                if len(v) >= 67:
                    nlen = v[64]
                    nspace = v[65]
                    nm = v[66:66 + nlen * 2].decode("utf-16-le", "replace")
                    names.append((nspace, nm,
                                  struct.unpack_from("<Q", v, 0)[0] & MASK,
                                  struct.unpack_from("<Q", v, 40)[0],
                                  struct.unpack_from("<Q", v, 48)[0]))
        elif atype == ATTR_DATA and data is None:
            if nonres:
                rsoff = struct.unpack_from("<H", rec, off + 32)[0]
                data = ("n", parse_runlist(rec, off + rsoff),
                        struct.unpack_from("<Q", rec, off + 48)[0],
                        bool(rec[off + 12] & 1))
            else:
                vlen = struct.unpack_from("<I", rec, off + 16)[0]
                voff = struct.unpack_from("<H", rec, off + 20)[0]
                data = ("r", rec[off + voff:off + voff + vlen].hex(), vlen, False)
        off += alen
    return dict(index=index, in_use=bool(flags & 1), is_dir=bool(flags & 2),
                names=names, data=data)


def choose_name(names):
    """Prefer the long name (namespace 1 or 3) over the 8.3 short name."""
    if not names:
        return None
    for want in (1, 3, 0, 2):
        for nspace, nm, pfrn, alloc, real in names:
            if nspace == want:
                return nm, pfrn, alloc, real
    nspace, nm, pfrn, alloc, real = names[0]
    return nm, pfrn, alloc, real


def validate_runs(runs, alloc, real, total_clusters):
    total = 0
    for lcn, length in runs:
        if length < 1 or length > MAX_RUN_CLUSTERS:
            return False, 0
        if lcn is not None and (lcn < 0 or lcn >= total_clusters):
            return False, 0
        total += length * CLUSTER
    if total > MAX_TOTAL_BYTES:
        return False, 0
    if alloc and total > alloc + CLUSTER * 4:
        return False, 0
    if real and total < real - CLUSTER * 4:
        return False, 0
    return True, total


# ------------------------------------------------------------------ main flow
def collect(disk, geo, part_off, progress=True):
    """Scan $MFT; return (deleted_files, deleted_dirs, alive_dirs)."""
    names, parents = {}, {}
    dead_files, dead_dirs = [], []
    chunk = 32 * 1024 * 1024
    read = 0
    t0 = time.time()
    while read < geo["nrec"] * geo["rec_size"]:
        blob = read_mft(disk, geo, read, chunk)
        if not blob:
            break
        cnt = len(blob) // geo["rec_size"]
        for k in range(cnt):
            idx = read // geo["rec_size"] + k
            rec = parse_record(blob[k * geo["rec_size"]:(k + 1) * geo["rec_size"]], idx)
            if not rec:
                continue
            picked = choose_name(rec["names"])
            if not picked:
                continue
            nm, pfrn, alloc, real = picked
            if rec["is_dir"]:
                names[idx] = nm
                parents[idx] = pfrn
            elif not rec["in_use"]:
                dead_files.append((idx, pfrn, nm, alloc, real, rec["data"]))
        read += cnt * geo["rec_size"]
        if progress and read % (256 * 1024 * 1024) < chunk:
            print("  scanned %.0f%% (%d deleted files) %.0fs"
                  % (100.0 * read / (geo["nrec"] * geo["rec_size"]), len(dead_files), time.time() - t0),
                  file=sys.stderr, flush=True)
    # rebuild parent chain: directories are in `names` only if deleted-dir or alive-dir
    return dead_files, names, parents


def build_paths(dead_files, dir_names, dir_parents, volume_root="C:"):
    """Resolve each deleted file's full path from parent references.

    The volume root's $FILE_NAME carries the name "." and a parent reference
    that points at itself, so the chain terminates there. Swap that "." for the
    drive letter to get an absolute path.
    """
    cache = {}

    def path_of(frn):
        frn &= MASK
        if frn in cache:
            return cache[frn]
        chain, cur, seen = [], frn, set()
        while cur in dir_names and cur not in seen:
            seen.add(cur)
            chain.append(dir_names[cur])
            cur = dir_parents.get(cur, cur) & MASK
        if chain and chain[-1] in (".", ""):
            chain[-1] = volume_root
        p = "\\".join(reversed(chain))
        cache[frn] = p
        return p

    out = []
    for idx, pfrn, nm, alloc, real, data in dead_files:
        parent = path_of(pfrn)
        full = parent + "\\" + nm if parent else volume_root + "\\" + nm
        # size: prefer the $DATA real size, then the run-list total, then $FILE_NAME
        dsize = data[2] if (data and data[0] == "n") else None
        if data and data[0] == "n":
            run_bytes = sum(l * CLUSTER for _, l in data[1])
        elif data and data[0] == "r":
            run_bytes = data[2]
        else:
            run_bytes = 0
        size = real or dsize or run_bytes
        out.append(dict(index=idx, path=full, name=nm, parent=pfrn,
                        alloc=alloc, real=size, run_bytes=run_bytes,
                        name_size=real, data=data))
    return out


def extract(disk, geo, part_off, items, out_dir, verify=False, resume=True):
    total_clusters = geo["total_sectors"] * 512 // geo["cluster"]
    ok = bad = skipped = written = 0
    bad_bytes = 0
    for n, it in enumerate(items):
        data = it["data"]
        if not data:
            continue
        size = it["real"]
        if data[0] == "r":
            payload = bytes.fromhex(data[1])
            size = size or len(payload)
        else:
            good, total = validate_runs(data[1], it["alloc"], it["name_size"],
                                       total_clusters)
            if not good:
                bad += 1
                continue
            size = it["real"] or total
        dest = os.path.join(out_dir, it["path"].replace("\\", os.sep))
        if resume and os.path.exists(dest) and os.path.getsize(dest) == size:
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            if data[0] == "r":
                with open(dest, "wb") as f:
                    f.write(payload)
            else:
                with open(dest, "wb") as f:
                    for lcn, length in data[1]:
                        nb = length * CLUSTER
                        if lcn is None:
                            while nb > 0:
                                chunk = b"\0" * min(nb, 4 * 1024 * 1024)
                                f.write(chunk)
                                nb -= len(chunk)
                        else:
                            off = part_off + lcn * CLUSTER
                            while nb > 0:
                                want = min(nb, 8 * 1024 * 1024)
                                blob = disk.read(off, want)
                                if not blob:
                                    f.write(b"\0" * want)
                                    break
                                f.write(blob)
                                off += len(blob)
                                nb -= len(blob)
                    if size:
                        f.truncate(size)
            written += size
            if verify:
                ext = os.path.splitext(dest)[1].lower()
                sigs = MAGIC.get(ext)
                if sigs:
                    with open(dest, "rb") as f:
                        head = f.read(32)
                    if not any(head.startswith(s) for s in sigs):
                        bad += 1
                        bad_bytes += size
                        continue
            ok += 1
        except Exception as exc:                       # noqa: BLE001
            bad += 1
            with open(os.path.join(out_dir, "_errors.log"), "a", encoding="utf-8") as lg:
                lg.write("%s :: %s\n" % (it["path"], exc))
        if n % 2000 == 0:
            print("  %d/%d ok=%d bad=%d skipped=%d %.1fGB"
                  % (n, len(items), ok, bad, skipped, written / 1e9),
                  file=sys.stderr, flush=True)
    return ok, bad, skipped, written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drive", required=True, help="drive letter, e.g. E")
    ap.add_argument("--out", help="destination directory (must be a different volume)")
    ap.add_argument("--list", action="store_true", help="inventory only")
    ap.add_argument("--match", action="append", default=[],
                    help="regex filter on the full path (repeatable)")
    ap.add_argument("--ext", action="append", default=[], help="extension filter")
    ap.add_argument("--min-size", type=int, default=0, help="bytes")
    ap.add_argument("--max-size", type=int, default=0, help="bytes (0 = no limit)")
    ap.add_argument("--by-size-desc", action="store_true",
                    help="order by size descending (fastest to get the big files)")
    ap.add_argument("--verify", action="store_true", help="check magic bytes")
    ap.add_argument("--index-out", help="write the inventory as JSON")
    args = ap.parse_args()

    device, part_off = drive_to_device(args.drive)
    print("device=%s partition_offset=%d" % (device, part_off), file=sys.stderr)
    disk = RawDisk(device)
    geo = read_geometry(disk, part_off)
    geo = mft_map(disk, part_off, geo)
    print("cluster=%d rec_size=%d records=%d" % (geo["cluster"], geo["rec_size"], geo["nrec"]),
          file=sys.stderr)

    dead_files, dir_names, dir_parents = collect(disk, geo, part_off)
    items = build_paths(dead_files, dir_names, dir_parents, volume_root=args.drive + ":")
    print("deleted files with a recoverable record: %d" % len(items), file=sys.stderr)

    rx = [re.compile(p, re.I) for p in args.match]
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in args.ext}

    def keep(it):
        if rx and not any(r.search(it["path"]) for r in rx):
            return False
        if exts and os.path.splitext(it["name"])[1].lower() not in exts:
            return False
        size = it["real"] or 0
        if args.min_size and size < args.min_size:
            return False
        if args.max_size and size > args.max_size:
            return False
        return True

    items = [it for it in items if keep(it)]
    if args.by_size_desc:
        items.sort(key=lambda it: -(it["real"] or 0))
    else:
        # ascending LCN => near-sequential disk reads
        def lcn_of(it):
            if it["data"] and it["data"][0] == "n" and it["data"][1]:
                return it["data"][1][0][0] or 0
            return 0
        items.sort(key=lcn_of)

    total = sum(it["real"] or 0 for it in items)
    print("selected %d files, %.2f GB" % (len(items), total / 1e9), file=sys.stderr)

    if args.index_out:
        with open(args.index_out, "w", encoding="utf-8") as f:
            json.dump([{k: v for k, v in it.items() if k != "data"} for it in items], f, indent=1)

    if args.list:
        for it in items[:2000]:
            print("%12d  %s" % (it["real"] or 0, it["path"]))
        print("... %d total" % len(items))
    elif args.out:
        if os.path.abspath(args.out).upper().startswith(args.drive.upper() + ":"):
            raise SystemExit("refusing to write to the source volume")
        os.makedirs(args.out, exist_ok=True)
        ok, bad, skipped, written = extract(disk, geo, part_off, items, args.out,
                                           verify=args.verify)
        print("done ok=%d bad=%d skipped=%d written=%.2fGB" % (ok, bad, skipped, written / 1e9))
    disk.close()


if __name__ == "__main__":
    main()
