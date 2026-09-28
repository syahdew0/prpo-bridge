#!/bin/sh
# Pasang prpo-bridge sebagai layanan launchd: hidup saat login, hidup lagi
# setelah crash, dan tidak mati saat terminal ditutup.
#
#   ./install-service.sh            pasang (atau perbarui) lalu jalankan
#   ./install-service.sh --dry-run  tampilkan plist-nya saja, tidak memasang
#   ./install-service.sh --uninstall  hentikan dan lepas layanannya
#
# Rahasia TIDAK ikut masuk plist: layanannya menjalankan run.sh, yang membaca
# .env seperti biasa. Berkas plist di ~/Library/LaunchAgents bisa dibaca proses
# lain, jadi kunci tidak boleh tinggal di sana.

set -e
DIR=$(cd "$(dirname "$0")" && pwd)
LABEL=id.psggroup.prpo-bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.prpo-bridge/logs"

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "layanan dilepas. Berkas .env, sesi, dan log tidak dihapus."
  exit 0
fi

[ -f "$DIR/.env" ] || { echo "ERROR: $DIR/.env belum ada — salin dari config.example.env dulu"; exit 1; }

# PATH ditulis eksplisit: launchd tidak mewarisi PATH milik shell-mu, dan
# `hermes` ada di ~/.local/bin yang tidak pernah masuk PATH bawaan launchd.
# Tanpa ini bridge jalan normal tapi setiap giliran gagal dengan
# "perintah 'hermes' tidak ditemukan".
PLIST_BODY=$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>$DIR/run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOGDIR/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGDIR/bridge.error.log</string>
</dict>
</plist>
PLIST
)

if [ "$1" = "--dry-run" ]; then
  echo "$PLIST_BODY"
  exit 0
fi

if lsof -nP -iTCP:"${BRIDGE_PORT:-8787}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port ${BRIDGE_PORT:-8787} sedang dipakai."
  echo "Hentikan bridge yang jalan manual (Ctrl-C di terminalnya) lalu ulangi."
  exit 1
fi

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"
printf '%s\n' "$PLIST_BODY" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || { echo "ERROR: plist tidak sah"; exit 1; }

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

echo "terpasang: $PLIST"
echo "log      : $LOGDIR/bridge.log"
echo
sleep 2
TOKEN=$(grep '^BRIDGE_TOKEN=' "$DIR/.env" | cut -d= -f2)
PORT=$(grep '^BRIDGE_PORT=' "$DIR/.env" | cut -d= -f2); PORT=${PORT:-8787}
if curl -s -m 5 -o /dev/null "http://127.0.0.1:$PORT/health?token=$TOKEN"; then
  echo "bridge menjawab di port $PORT — layanan berjalan."
else
  echo "bridge BELUM menjawab. Lihat: tail -f $LOGDIR/bridge.error.log"
fi
