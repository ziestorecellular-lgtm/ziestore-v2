# ZIESTORE — Tahap 2: Login & Hak Akses Dasar

## Fitur
- Setup awal membuat akun Owner tanpa password default.
- Kata sandi disimpan menggunakan PBKDF2-HMAC-SHA256.
- Login, logout, dan sesi 8 jam.
- Owner dapat membuat akun Kasir dari menu Pengguna.
- Kasir tidak dapat menambah produk, stok IMEI, atau mengimpor katalog.
- Database lokal tetap berada di file `ziestore.db`.

## Menjalankan
1. Ekstrak ZIP.
2. Buka folder berisi `app.py`, ketik `cmd` pada address bar File Explorer lalu tekan Enter.
3. Jalankan `python app.py` atau `py app.py`.
4. Buka `http://127.0.0.1:8080`.
5. Buat akun Owner dengan nama pengguna 4–40 karakter dan kata sandi minimal 12 karakter.
6. Masuk menu Pengguna untuk membuat akun Kasir.

## Backup
Hentikan aplikasi dengan Ctrl+C, lalu salin `ziestore.db` ke lokasi aman.

## Batasan penting
Ini prototipe lokal, bukan aplikasi produksi. Belum tersedia HTTPS, perlindungan CSRF lengkap, pembatasan percobaan login, reset password, audit lengkap, multi-cabang penuh, sinkronisasi online/offline, akuntansi lengkap, PDF, atau file `.xlsx`. Jangan buka ke internet dan jangan masukkan data pelanggan sensitif.
