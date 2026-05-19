# VPN Helper

Forti mu suka down? Suka mengambil alih jendela yang sedang tampil? Suka tiba-tiba membuka jendela baru padahal lagi fokus kerja? Susah ditakedown?

Coba pake ini. Aku ga jamin anti down, tapi ini ga semaruk Forti dalam menggunakan resource dan jaringan PC mu. Also, dia ga akan ujug-ujug buka tab SSO ketika koneksi VPN naik-turun.

[Langsung ke Persiapan](#persiapan)

---
## Ini Apa?
Intinya ini VPN pengganti Forticlient yang ngaturnya di browser.

---
## Extras
VPN itu pada dasarnya bikin tunnel: jalur khusus dari komputer kita ke jaringan tujuan. Aplikasi ini ngurus login SSO dan sesi gateway-nya, tapi tetap butuh mesin tunnel yang beneran mengangkat traffic jaringan. Mesin itu namanya OpenConnect.

Silakan ikuti langkah menginstal OpenConnect di section [**Persiapan**](#persiapan).

---
## Persiapan

1. Clone/download zip repo ini
2. Pastikan python sudah terinstal (biasalah, seluruh app ku pake python)
3. Install OpenConnect

   Windows:
   Download installer OpenConnect-GUI dari [release resminya](https://github.com/openconnect/openconnect-gui/releases/download/v1.5.3/openconnect-gui-1.5.3-win32.exe), lalu install seperti aplikasi biasa.

   Ubuntu/Debian:
   ```bash
   sudo apt install openconnect
   ```

   Fedora:
   ```bash
   sudo dnf install openconnect
   ```

   Arch:
   ```bash
   sudo pacman -S openconnect
   ```

   macOS:
   ```bash
   brew install openconnect
   ```

4. Klik `requirements.bat` (Windows) atau jalankan `sh requirements.sh` (Mac/Linux) untuk menginstal dependensi yang dibutuhkan. Kalau dependensinya sudah ada, dia tidak install ulang. Kalau sukses, file installer itu akan menghapus dirinya sendiri setelah 3 detik. Kalau Python belum terinstal, dia cuma ngasih tahu lalu keluar tanpa menghapus dirinya.
5. Download file `.env` dari Git BPS ku di [sini](https://git.bps.go.id/gilangprasetyo/vpn-helper), lalu taruh di root folder project ini.
```info
Kenapa .env kupisah ke Git BPS? Isinya konfigurasi sensitif. Walaupun semua orang bisa menguliknya sendiri dengan brainstorming, namun setidaknya bukan karna lihat repoku. 
```
6. Klik `run.bat` (Windows) atau jalankan `sh run.sh` (Mac/Linux) untuk menjalankan aplikasi.
7. Akses di http://localhost:8765
8. Ada 3 status di aplikasi yang menandakan VPN aktif ketika tiga-tiganya aktif(berwarna hijau): SSO, VPN dan Gateway. Gausah bingung, tombol connect akan membuat ketiganya aktif serempak, demikian tombol disconnect membuat ketiganya mati.
---

## License

This project is distributed under the VPN Helper Use-Only License.

Use of this software is free of charge. Modification, publication of modified versions, sublicensing, selling, renting, or repackaging requires prior written permission from the copyright holder.

See [LICENSE](LICENSE) for the full English and Indonesian license text.
