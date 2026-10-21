# 🛟 ntfs-file-recovery

Pulihkan file yang terhapus dari drive Windows **NTFS** — tanpa tool komersial,
tanpa backup, tanpa Shadow Copy. Plus forensik: **siapa** yang menghapusnya.

Berbasis teknik yang dipakai untuk memulihkan **742 GB / 189.949 file** dari
drive E: yang isinya wiped total oleh `rd /s /q` yang mengikuti junction.

| | |
| --- | --- |
| Bahasa | Python 3 (stdlib saja) + PowerShell |
| Dependensi | Tidak ada (`ctypes` saja untuk raw disk read) |
| Root | Admin / elevated shell wajib untuk akses `\\.\PhysicalDriveN` |
| Lisensi | MIT — lihat [`LICENSE`](LICENSE) |

## Kenapa skill ini ada

Pada 21 Oktober 2026 sebuah `cmd /c "rmdir /s /q ..."` dengan escaping rusak
menyapu **591 GB** dari drive E:. Tidak ada backup, tidak ada Shadow Copy, tidak
ada recycle bin. Satu-satunya sumber data yang masih utuh adalah **MFT** NTFS
itu sendiri.

Hasilnya: **99,1% file berhasil dipulihkan** (nama, path, dan isi utuh),
karena record MFT-nya belum sempat didaur ulang.

## Yang dicakup

1. **Forensik dulu** — USN Journal, log PowerShell, cek Shadow Copy, cek
   recycle bin, deteksi junction.
2. **Pemulihan dari `$MFT`** — parser NTFS dari nol: boot sector → `$MFT` →
   rekonstruksi path via parent reference → ekstrak isi dari run list.
3. **Validasi** — magic byte per ekstensi, Sampling hundreds of files and
   reporting the hit rate instead of claiming success.
4. **Footgun junction** — kenapa `rd /s /q` bisa menghapus SELURUH isi drive.

## Pemasangan (sebagai OpenCode skill)

Salin folder ini ke direktori skills OpenCode:

```powershell
Copy-Item -Recurse ntfs-file-recovery "$env:USERPROFILE\.config\opencode\skills\"
```

Atau pakai symlink biar tetap sinkron dengan repo:

```powershell
New-Item -ItemType SymbolicLink `
  -Path "$env:USERPROFILE\.config\opencode\skills\ntfs-file-recovery" `
  -Target "C:\path\to\ntfs-file-recovery"
```

## Pemakaian

### 1. Forensik (read-only, tidak perlu admin)

```powershell
.\scripts\triage.ps1 -Drive E
```

Menghasilkan: timeline USN Journal, perintah destruktif di log PowerShell,
status Shadow Copy/Windows Backup, integritas recycle bin, dan daftar
junction/reparse point.

### 2. Inventaris file yang terhapus

```powershell
python scripts\ntfs_recover.py --drive E --list
python scripts\ntfs_recover.py --drive E --list --match "\.pdf$" --match "DOKUMENTASI"
python scripts\ntfs_recover.py --drive E --list --index-out inventory.json
```

### 3. Ekstraksi

```powershell
# WAJIB elevated
python scripts\ntfs_recover.py --drive E --out F:\recovered
python scripts\ntfs_recover.py --drive E --out F:\recovered --by-size-desc --verify
```

> **Jangan pernah menulis ke drive sumber.** Tiap byte yang ditulis bisa
> menimpa cluster file yang belum sempat dipulihkan. Script menolak menulis ke
> drive yang sama.

## Cara kerjanya

| Tahap | Isi |
| --- | --- |
| Boot sector | bytes/sector, sectors/cluster, LCN `$MFT`, ukuran record |
| `$MFT` | run list sendiri (bisa multi-run) → peta offset |
| Record | header flag (in-use / directory), `$FILE_NAME`, `$DATA` |
| Path | naik lewat parent FRN;FRN = 48 bit index + 16 bit sequence → **mask `& 0xFFFFFFFFFFFF`** |
| Nama | pilih namespace 1/3 (panjang) sebelum namespace 2 (8.3) |
| Ukuran | dari run list, bukan `$FILE_NAME` (bisa basi / nol) |
| Validasi | panjang run ≤ 2^26 cluster, LCN dalam volume, total cocok dengan size |
| Extract | urut **LCN menaik** → baca disk nyaris sekuensial |

Detail teknis ada di [`reference/ntfs-layout.md`](reference/ntfs-layout.md).

## Jebakan yang harus diketahui

- **Junction + `rd /s /q` = hapus isi drive.**
  `cmd.exe` menelusuri *melewati* junction dan menghapus isi targetnya.
  `Remove-Item -Recurse` (PowerShell) menghapus link-nya saja.
  Kalau path targetnya terlihat seperti root drive — berhenti.
- **Powershell tidak meng-escape `\"` di dalam string berkoma dua.**
  `cmd /c "rmdir /s /q \"C:\path\""` terpotong jadi beberapa argumen.
  Pakai backtick (`` `" ``) atau `--%`.
- **Record MFT bisa didaur ulang** begitu slot-nya dipakai file baru.
  Quanto cepat recovery dimulai, semakin besar peluang datanya masih ada.
- **Checksum / magic byte harus dicek.** File dengan CRC/size cocok tapi isi
  sudah tertimpa akan lolos validasi nama.

## Studi kasus

Laporan lengkap insiden 21 Oktober 2026 (E: wiped 591 GB, 189.949 file, tanpa
backup): [`docs/laporan-insiden-2026-10-21.md`](docs/laporan-insiden-2026-10-21.md).

Timeline singkat:

| Waktu | Kejadian |
| --- | --- |
| 14:37:05 | agent menghapus folder proyek NEXUS (sesuai permintaan) |
| 14:37:45 | `cmd /c "rmdir /s /q \"...\""` → **timeout 60 detik** (masih menghapus) |
| 14:37:46 | drive E: mulai terkikis **urutan alfabetis** per folder top-level |
| 14:38:58 | `takeown` + `icacls` + ulangi `rmdir` (memperparah) |
| 14:41:16 | agent menyimpulkan "ALL PROJECT FILES SUCCESSFULLY CLEANED" |

Bukti: log `Windows PowerShell` (Event ID 400) memuat baris `HostApplication`
persis; USN Journal mencatat urutan walk; output `rd` menampilkan path
relatif terhadap *root drive* (`\Program Files\...`), ciri `rd /s` yang
menelusuri junction ke root.

## Lisensi

MIT © 2026 Ansel Caprico — pakai bebas, ganti sesuai kebutuhan.
