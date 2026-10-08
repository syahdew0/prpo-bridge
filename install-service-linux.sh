#!/bin/sh
# Pasang prpo-bridge sebagai layanan systemd di Linux.
#
#   sudo ./install-service-linux.sh                 pasang + jalankan (user = pemilik folder)
#   sudo ./install-service-linux.sh --user agent    jalankan sebagai user tertentu
#   ./install-service-linux.sh --dry-run            tampilkan unit-nya saja
#   sudo ./install-service-linux.sh --uninstall     hentikan dan lepas
#
# Padanan Linux dari install-service.sh (launchd). Dua hal yang membedakannya
# dari menjalankan ./run.sh begitu saja:
#
#   - Layanannya WAJIB berjalan sebagai user yang memiliki profil Hermes-nya.
#     Riwayat sesi, kredensial model, dan daftar profil semuanya per-user; dijalankan
#     sebagai root, bridge akan memanggil Hermes milik root yang kosong.
#   - PATH ditulis eksplisit. systemd tidak mewarisi PATH login, sedangkan wrapper
#     profil (hermes profile alias) biasanya tinggal di ~/.local/bin. Tanpa itu
#     bridge jalan normal tapi tiap giliran gagal "perintah tidak ditemukan".

set -e
DIR=$(cd "$(dirname "$0")" && pwd)
NAME=prpo-bridge
UNIT=/etc/systemd/system/$NAME.service
RUN_USER=""
MODE=install

while [ $# -gt 0 ]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --dry-run) MODE=dry; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    *) echo "argumen tidak dikenal: $1"; exit 1 ;;
  esac
done

if [ "$MODE" = uninstall ]; then
  systemctl stop $NAME 2>/dev/null || true
  systemctl disable $NAME 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload
  echo "layanan dilepas. Berkas .env, sesi, dan media tidak dihapus."
  exit 0
fi

[ -f "$DIR/.env" ] || { echo "ERROR: $DIR/.env belum ada — salin dari config.example.env dulu"; exit 1; }

# Default: pemilik folder, bukan root. Hampir selalu itu yang benar.
# stat -c milik GNU, -f milik BSD; getent tidak ada di macOS. Dibuat tahan
# keduanya supaya --dry-run bisa diperiksa dari laptop sebelum dibawa ke server.
[ -n "$RUN_USER" ] || RUN_USER=$(stat -c '%U' "$DIR" 2>/dev/null || stat -f '%Su' "$DIR" 2>/dev/null || id -un)
HOME_DIR=$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)
[ -n "$HOME_DIR" ] || HOME_DIR=$(eval echo "~$RUN_USER" 2>/dev/null)
case "$HOME_DIR" in ~*|"") echo "ERROR: home user '$RUN_USER' tidak ditemukan"; exit 1 ;; esac

UNIT_BODY=$(cat <<UNITEOF
[Unit]
Description=prpo-bridge — penghubung Satuchat Flow Builder ke Hermes Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
Environment=HOME=$HOME_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh $DIR/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
)

if [ "$MODE" = dry ]; then
  echo "# user   : $RUN_USER"
  echo "# home   : $HOME_DIR"
  echo "# unit   : $UNIT"
  echo
  echo "$UNIT_BODY"
  exit 0
fi

[ "$(id -u)" = 0 ] || { echo "ERROR: jalankan dengan sudo"; exit 1; }

PORT=$(grep '^BRIDGE_PORT=' "$DIR/.env" | cut -d= -f2); PORT=${PORT:-8787}
if ss -ltn 2>/dev/null | grep -q ":$PORT " ; then
  echo "ERROR: port $PORT sedang dipakai. Hentikan proses itu lalu ulangi."
  exit 1
fi

printf '%s\n' "$UNIT_BODY" > "$UNIT"
systemctl daemon-reload
systemctl enable --now $NAME

echo "terpasang: $UNIT  (user: $RUN_USER)"
echo "log      : journalctl -u $NAME -f"
echo
sleep 2
TOKEN=$(grep '^BRIDGE_TOKEN=' "$DIR/.env" | cut -d= -f2)
if curl -s -m 5 -o /dev/null "http://127.0.0.1:$PORT/health?token=$TOKEN"; then
  echo "bridge menjawab di port $PORT — layanan berjalan."
else
  echo "bridge BELUM menjawab. Lihat: journalctl -u $NAME -n 50"
fi
