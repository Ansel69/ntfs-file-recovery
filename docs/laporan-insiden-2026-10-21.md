# Laporan Insiden: Drive E: Terhapus Massal — 21 Oktober 2026

**Volume:** E: (HDD 931 GB, `ST1000DM003`, SMR) · **Skala:** 591,7 GB / 189.949 file
**Status akhir:** 742,5 GB / 249.161 file dipulihkan (99,1%)

---

## 1. Kronologi

| Waktu (WIB) | Kejadian | Sumber bukti |
| --- | --- | --- |
| 14:37:05 | Agent menghapus `E:\DOKUMEN\godot_project` + 2 ISO Nexus — **sesuai permintaan user** | USN Journal |
| 14:37:35 | Agent menjalankan script "Verify cleanup" | event log 400 |
| 14:37:40 | `Remove-Item "C:\Users\...\Temp\opencode" -Recurse -Force` | event log 400 |
| **14:37:45** | **`cmd /c "rmdir /s /q \"C:\Users\...\Temp\opencode\""`** → tool output: **"Timed out before completion"** (60 dtk, masih menghapus) | event log 400 + session DB |
| **14:37:46** | **Drive E: mulai terkikis**, urutan **alfabetis** per folder top-level | USN Journal |
| 14:38:03–14:38:58 | 3 ISO Nexus lain ikut terhapus (`Nexus_FINAL_original_blu.iso`, `Nexus_TIGA_2387_keep.iso`, `Nexus_TIGA_2387v_keep_plugins.iso`) | USN Journal |
| 14:38:58 | `takeown /f ... /r /d y` + `icacls ... /t` + ulangi `rmdir /s /q` | event log 400 |
| 14:40:00 | File inti `lazy-media` (source code + `.md` desain) terhapus | USN Journal |
| 14:40:58 | `E:\SteamLibrary` ikut terhapus (junction → isi `E:`) | USN Journal |
| 14:41:16 | Agent menyimpulkan "✅ ALL PROJECT FILES SUCCESSFULLY CLEANED" | session DB |

Total: 217.032 record `File delete` dalam 4 menit 12 detik.

---

## 2. Akar masalah

### 2.1 Dua hal terpisah, bukan satu

Permintaan user ("hapus proyek game NEXUS") **dijalankan dengan benar** pada
14:37:05 — hanya `E:\DOKUMEN\godot_project` + 2 ISO. Yang menghapus SELURUH
E: adalah perintah **berikutnya**, 40 detik kemudian, yang tidak ada hubungannya
dengan permintaan itu.

### 2.2 Escaping rusak di PowerShell

```
cmd /c "rmdir /s /q \"C:\Users\Ansel\AppData\Local\Temp\opencode\""
```

PowerShell **tidak** memakai backslash sebagai escape character di dalam string
berkoma dua (backtick yang dipakai). Jadi `\"` mengakhiri string lebih awal:

| Token | Hasil parse |
| --- | --- |
| `"rmdir /s /q \"` | string berisi `rmdir /s /q \` |
| `C:\Users\...\opencode\` | argumen terpisah |
| `""` | string kosong |

Argumen yang sampai ke `cmd.exe` jadi berantakan, dan target yang dihapus
menjadi **dll.**, bukan folder yang dimaksud.

### 2.3 Junction + `rd /s /q` = hapus isi drive

`cmd.exe` (`rd /s /q`) **menelusuri melintasi** directory junction dan
menghapus **isi targetnya**. `Remove-Item -Recurse` (PowerShell) menghapus
link-nya saja, tidak isi target.

Di dalam `C:\Users\Ansel\AppData\Local\Temp\opencode\` ada junction yang
menunjuk ke root E:. `rd /s /q` menelusurinya → seluruh E: terhapus.

**Bukti yang membedakan keduanya** — USN Journal mencatat path yang
melewati junction:

```
E:\GAME\YostarGames\Arknights_EN\Arknights_Data\...
```

s sementara parent reference di MFT menunjuk ke `E:\DOKUMEN\Arknights_EN`.
Dua-duanya benar: yang pertama lewat junction, yang kedua lokasi fisiknya.
Inilah cara memastikan sebuah junction ikut berperan.

Output `rd` juga mengonfirmasi: error-nya memakai path **relatif terhadap root
drive**:

```
cmd : \Program Files\Adobe\Adobe Creative Cloud Experience\CCXProcess.exe - Access is denied.
```

Path relatif root seperti ini hanya muncul kalau `rd` sedang menelusuri
suatu drive root.

### 2.4 File yang selamat = file yang terkunci

`E:\Program Files\Adobe\`, `E:\_CACHE\node_modules`, `E:\_ORGANISASI`,
`E:\Docker` — semuanya gagal dihapus karena prosesnya masih berjalan
("Access is denied"). Pola ini konfirmasi: yang hilang adalah yang **tidak
terkunci**, bukan yang dipilih.

---

## 3. Dampak

| Top-level | Ukuran | File |
| --- | --- | --- |
| `DOKUMENTASI KEGIATAN` | 375 GB | 13.942 |
| `Games` (StellaSora) | 84 GB | 16.566 |
| `BAHAN KERJA` | 64 GB | 6.375 |
| `_ASET KERINCI` | 47 GB | 2.676 |
| `DOKUMEN` | 36 GB | 33.397 |
| `LAINNYA` | 36 GB | 12.866 |
| `Program Files` | 17 GB | 27.998 |
| `SteamLibrary` | 17 GB | 2.067 |
| `PROYEK` | 0,5 GB | 15.714 |
| `FACEBOOK` | 1,8 GB | 2.915 |
| `KAMERA`, `HMI`, `open-notebook` | 2,5 GB | 827 |

---

## 4. Pemulihan

### 4.1 Kenapa masih bisa dipulihkan

Tidak ada backup (Windows Backup: *"No backup was found"*), tidak ada Shadow
Copy, tidak ada recycle bin. Tapi **record MFT NTFS tidak pernah ditulis ulang**
oleh proses `rd` — menghapus file hanya menandai record-nya "not in use" dan
membebaskan cluster-nya. Nama file, ukuran, dan **run list cluster** masih utuh
di sana.

### 4.2 Cara

1. Baca boot sector → bytes/sector, sectors/cluster, LCN `$MFT`, ukuran record
2. Baca run list `$MFT` dari record 0 (bisa multi-run) → peta offset
3. Scan 483.840 record MFT → ambil record yang flag "not in use"
4. Rekonstruksi path: naik lewat parent FRN sampai root
5. Ekstrak isi: jalankan run list → baca cluster langsung dari disk
6. Validasi: magic byte per ekstensi


具体修法（把两处残缺标签补全）：

**第一处**（开头缺失 ``）：

```
Scan 483.840 record MFT → ambil record yang flag "not in use"
```

**第二处**（结尾缺失 `</p>`）：

```
6. Validasi: magic byte per ekstensi</p>
```

---

### 4.3 Hasil

| | |
| --- | --- |
| Berhasil dipulihkan | **188.236 file** di path aslinya + 2.525 file yang pindah saat penataan |
| Tidak bisa dipulihkan | 1.711 file / 47 GB → 99% **bisa diunduh ulang** (file `.bdt` Sekiro, core DLL Arknights & StellaSora, model ollama, `vhdx` Docker, cache npm) |
| Benar-benar hilang | 2 file video DJI (814 MB) + arsip RAR WISATA KERINCI (isi-nya masih ada di `I:\WISATA KERINCI 2025`) |
| Validasi sampel 200 file | JPG/JPEG/PNG/PDF ~100% valid |

---

## 5. Pelajaran

1. **`rd /s /q` dan `Remove-Item -Recurse` berbeda di junction.** Selalu
   enumerasi reparse point sebelum recursive delete.
2. **Escaping PowerShell ≠ C.** `"...\""` bukan escape; pakai backtick.
3. **Hapus itu destruktif dan tidak bisa di-undo.** Cek ulang path target sebelum
   menjalankan, dan pakai `Remove-Item` (PowerShell) alih-alih `rd` (cmd) untuk
   aman terhadap junction.
4. **Backup itu bukan opsional.**案例 ini 742 GB kembali **karena tidak ada
   satu pun file yang ditulis ulang di drive itu selama ~2 jam**. begitu ada
   file besar yang ditulis, MFT record langsung didaur ulang.
5. **Jangan menulis ke drive sumber saat recovery.** Writer dan reader berebut
   cluster yang sama.

---

## 6. Reproduksi

Repo ini berisi dua skrip yang dipakai:

- `scripts/triage.ps1` — forensik read-only
- `scripts/ntfs_recover.py` — parser `$MFT` + ekstraktor
- `../reference/ntfs-layout.md` — detail struktur NTFS
