# prpo-bridge

Menyambungkan cabang **Tools PR/PO** di Satuchat Flow Builder ke agen PR/PO
(Hermes + plugin `psg-po-pr`) yang selama ini dipakai lewat Telegram. Tim
purchasing mengobrol lewat WhatsApp ke PSG Help Center, dan menerima berkas
`.xlsx` + `.pdf` sebagai dokumen WhatsApp — sama seperti di Telegram.

Telegram tidak ada di jalur ini. Bot Telegram tidak boleh mengirim pesan ke bot
lain, dan satu akun relay akan membuat semua nomor WA berbagi satu riwayat
percakapan. Bridge memanggil agen yang sama secara langsung, dengan sesi
terpisah per nomor.

## Bentuk alurnya

```
WhatsApp ─► Satuchat Flow ─(API Call)─► bridge ─► hermes chat -c wa-628xxx
                                          │              └─ plugin psg-po-pr
                                          │                  generate_pr / generate_po
WhatsApp ◄── Satuchat ◄──(/api/v1/bridge)─┘   teks + .xlsx + .pdf
```

Node API Call hanya menunggu 10 detik, sedangkan satu giliran agen butuh 8–60
detik. Karena itu bridge menjawab `202` seketika, lalu mendorong balasannya
belakangan lewat Bridge API di backend Satuchat.

## Isi folder

| Berkas | Isi |
|---|---|
| `bridge.py` | servicenya (pustaka standar Python 3.10+, tanpa dependensi) |
| `selfcheck.py` | uji tanpa Hermes & Satuchat sungguhan — jalankan kapan saja |
| `build_flow.py` | hasilkan canvas Flow Builder; **di sinilah URL, token, dan daftar nomor diisi** |
| `validate_flow.py` | periksa canvas terhadap aturan nyata flow-executor |
| `apply_flow.py` | pasang canvas ke flow lewat API (dry-run kecuali `--yes`) |
| `run.sh` | jalankan bridge dengan `.env` |
| `config.example.env` | contoh konfigurasi |

## Pasang

### 1. Backend Satuchat

Di `.env` backend, isi kunci minimal 32 karakter:

```
BRIDGE_API_KEY=<acak, minimal 32 karakter>
```

Tanpa kunci ini seluruh Bridge API menjawab `503` — mati secara default.
Restart backend, lalu cek:

```bash
curl -H "x-api-key: $BRIDGE_API_KEY" https://api-satuchat-kamu/api/v1/bridge/health
# {"success":true,"data":{"ok":true}}
```

### 2. Bridge

```bash
cp config.example.env .env   # isi BRIDGE_TOKEN, SATUCHAT_API_BASE, SATUCHAT_BRIDGE_KEY
python3 selfcheck.py         # harus hijau semua
./run.sh
```

### 3. Tunnel HTTPS

Node API Call menolak `http`, `localhost`, dan semua alamat privat. Bridge harus
punya alamat https publik:

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

Catat URL `https://xxx.trycloudflare.com` yang muncul.

> URL tunnel percobaan berubah setiap restart. Untuk pemakaian harian, pakai
> named tunnel dengan domain tetap — kalau tidak, URL di node API Call harus
> diganti tiap kali bridge dinyalakan ulang.

### 4. Canvas flow

Buka `build_flow.py`, isi bagian paling atas:

```python
BRIDGE_URL      = "https://xxx.trycloudflare.com"
BRIDGE_TOKEN    = "sama dengan BRIDGE_TOKEN di .env"
NOMOR_DIIZINKAN = ["+628...", "+628..."]   # tim purchasing
```

Lalu:

```bash
python3 build_flow.py
python3 validate_flow.py flow-psg-help-center.json   # harus "lolos semua pemeriksaan"
python3 apply_flow.py --base ... --org ... --flow ... --token ...        # dry-run
python3 apply_flow.py --base ... --org ... --flow ... --token ... --yes  # timpa canvas
```

`apply_flow.py --yes` **menimpa seluruh canvas** flow tujuan. Kalau ragu, susun
manual di Flow Builder mengikuti struktur di `flow-psg-help-center.json` —
hasilnya sama.

Terakhir tekan **Publish** di Flow Builder: percakapan sungguhan memakai versi
yang di-publish, bukan draft.

## Cara kerja sesi

Nama sesi Hermes = `wa-<nomor>`. Satu nomor = satu riwayat, jadi dua staff yang
mengerjakan PR berbeda tidak pernah tercampur.

Tiap kali pelanggan memilih "Tools PR PO" di menu, flow memanggil `/wa/start`
dan sesi lama dibuang — PR yang tidak selesai kemarin tidak ikut ke PO hari ini.
Pemetaan nomor → sesi disimpan di `~/.prpo-bridge/sessions.json`.

Pesan dari satu nomor diproses berurutan; nomor berbeda berjalan paralel.

## API

Dipanggil Satuchat (token di URL karena node API Call tidak bisa kirim header):

```
POST /wa/start?token=…    {"session":"628…"}                        → 200
POST /wa/message?token=…  {"session":"628…","conversationId":"…",
                           "text":"…"}                              → 202
GET  /health?token=…                                                → 200
```

Dipanggil bridge ke Satuchat (kunci di header):

```
POST /api/v1/bridge/messages   x-api-key   {"conversationId":"…","text":"…"}
POST /api/v1/bridge/media      x-api-key   multipart: conversationId, caption, file
```

Bridge berhenti mengirim kalau Satuchat menjawab `409`: percakapan ditutup, atau
sudah diambil agent manusia. Antrean yang tersisa untuk sesi itu dibuang.

## Kalau ada yang gagal

| Gejala | Kemungkinan |
|---|---|
| Node API Call selalu ke port Gagal | tunnel mati, URL berubah, atau token di URL tidak cocok |
| Bridge jalan tapi balasan tidak muncul di WA | `SATUCHAT_BRIDGE_KEY` ≠ `BRIDGE_API_KEY`, atau `SATUCHAT_API_BASE` salah — lihat log saat start |
| Balasan masuk, berkas tidak | cek log `push_file gagal`; PDF gagal dibuat kalau LibreOffice tidak ada |
| Pelanggan dapat pesan teknis "Warning: …" | keluaran Hermes berubah bentuk — periksa `_parse_output` dan jalankan `selfcheck.py` |
| Semua diam | percakapan sudah diambil agent (`409 handled_by_agent`) — itu memang disengaja |

Log bridge menulis ke stdout: satu baris per giliran agen, lengkap dengan durasi
dan jumlah berkas.
