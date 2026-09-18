#!/bin/sh
# Jalankan bridge dengan konfigurasi dari .env di folder ini.
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "Buat .env dulu — salin dari config.example.env"; exit 1; }
set -a
. ./.env
set +a
exec python3 bridge.py
