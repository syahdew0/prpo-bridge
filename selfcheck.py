#!/usr/bin/env python3
"""
Selfcheck prpo-bridge — jalankan dengan: python3 selfcheck.py

Tanpa Hermes sungguhan dan tanpa Satuchat sungguhan: keduanya diganti tiruan,
supaya bagian yang paling mudah rusak bisa diuji kapan saja dalam hitungan
detik — pemisahan teks dari berkas, pemotongan pesan panjang, penolakan token,
dan jalur lengkap "pesan masuk → agen → teks + berkas sampai di Satuchat".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="prpo-selfcheck-"))
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# ── Satuchat tiruan ──────────────────────────────────────────────────────────

RECEIVED: dict[str, list] = {"text": [], "media": []}
API_KEY = "kunci-satuchat-tiruan-yang-cukup-panjang"


class FakeSatuchat(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:
        pass

    def _read(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._json(200, {"success": True, "data": {"ok": True}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self._read()
        if self.headers.get("x-api-key") != API_KEY:
            return self._json(401, {"success": False, "message": "kunci salah"})
        if self.path.endswith("/messages"):
            RECEIVED["text"].append(json.loads(raw))
        else:
            RECEIVED["media"].append(raw)
        self._json(200, {"success": True, "data": {"messageId": "m1", "delivered": True}})


def start_fake_satuchat() -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSatuchat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


# ── Hermes tiruan ────────────────────────────────────────────────────────────

PO_FILE = WORK / "PO_FRM-PO-IT-2026-IX-1.pdf"
PO_FILE.write_bytes(b"%PDF-1.4 dummy")

FAKE_HERMES = WORK / "hermes-tiruan"
FAKE_HERMES.write_text(
    "#!/bin/sh\n"
    "cat > /dev/null\n"                       # pesan pengguna lewat stdin
    "echo 'Warning: Unknown toolsets: psg_po_pr'\n"
    "echo 'PO berhasil dibuat.'\n"
    f"echo 'MEDIA:{PO_FILE}'\n"
    "echo 'session_id: x' 1>&2\n"
)
FAKE_HERMES.chmod(0o755)

BASE = start_fake_satuchat()
os.environ.update({
    "BRIDGE_TOKEN": "token-bridge-tiruan",
    "SATUCHAT_API_BASE": BASE,
    "SATUCHAT_BRIDGE_KEY": API_KEY,
    "HERMES_BIN": str(FAKE_HERMES),
    "BRIDGE_STATE_FILE": str(WORK / "sessions.json"),
    "BRIDGE_PORT": "0",
    "BRIDGE_LOG_LEVEL": "CRITICAL",
})

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge  # noqa: E402


# ── 1. Pemisahan teks dan berkas ─────────────────────────────────────────────

print("\nparsing keluaran hermes")

text, files = bridge._parse_output(
    "Warning: Unknown toolsets: psg_po_pr\n"
    "PO berhasil dibuat.\n"
    f"MEDIA:{PO_FILE}\n"
)
check("baris Warning tidak ikut terkirim", "Warning" not in text, repr(text))
check("teks bersih", text == "PO berhasil dibuat.", repr(text))
check("berkas terdeteksi", files == [PO_FILE], str(files))

text, files = bridge._parse_output(f"Ini hasilnya: {PO_FILE} silakan dicek")
check("path tanpa penanda MEDIA tetap terbaca", files == [PO_FILE], str(files))
check("path dibuang dari teks", str(PO_FILE) not in text, repr(text))

_, files = bridge._parse_output("MEDIA:/tidak/ada/berkas.pdf")
check("path yang tidak ada diabaikan", files == [], str(files))

_, files = bridge._parse_output(f"MEDIA:{PO_FILE}\nMEDIA:{PO_FILE}")
check("berkas ganda tidak dikirim dua kali", files == [PO_FILE], str(files))


# ── 2. Pemotongan pesan panjang ──────────────────────────────────────────────

print("\npemotongan teks panjang")

long_text = "\n".join(f"baris ke-{i} dengan isi yang cukup panjang" for i in range(300))
chunks = bridge._chunk(long_text, 500)
check("semua potongan di bawah batas", all(len(c) <= 500 for c in chunks), str([len(c) for c in chunks]))
check("tidak ada isi yang hilang", "baris ke-299" in chunks[-1])
check("teks kosong tidak menghasilkan potongan", bridge._chunk("   ", 100) == [])


# ── 3. HTTP: token dan rute ──────────────────────────────────────────────────

print("\nhttp")

httpd = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BRIDGE_URL = f"http://127.0.0.1:{httpd.server_address[1]}"


def call(path: str, payload: dict | None = None, token: str = "token-bridge-tiruan") -> tuple[int, dict]:
    url = f"{BRIDGE_URL}{path}?token={token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


status, _ = call("/health", token="token-salah")
check("token salah ditolak", status == 401, str(status))

status, _ = call("/health")
check("token benar diterima", status == 200, str(status))

status, _ = call("/wa/message", {"session": "wa-628", "conversationId": "c1"})
check("pesan tanpa teks ditolak", status == 400, str(status))

status, body = call("/wa/message", {"session": "wa-628", "conversationId": "c1", "text": "buatkan PO"})
check("pesan diterima langsung (tidak menunggu agen)", status == 202, f"{status} {body}")


# ── 4. Ujung ke ujung ────────────────────────────────────────────────────────

print("\nujung ke ujung")

deadline = time.time() + 15
while time.time() < deadline and not (RECEIVED["text"] and RECEIVED["media"]):
    time.sleep(0.1)

check("teks balasan sampai ke Satuchat", len(RECEIVED["text"]) == 1, str(RECEIVED["text"]))
if RECEIVED["text"]:
    check("isinya jawaban agen, bukan catatan internal",
          RECEIVED["text"][0]["text"] == "PO berhasil dibuat.", str(RECEIVED["text"][0]))
    check("percakapan yang dituju benar", RECEIVED["text"][0]["conversationId"] == "c1")
check("berkas PDF ikut terkirim", len(RECEIVED["media"]) == 1, str(len(RECEIVED["media"])))
if RECEIVED["media"]:
    blob = RECEIVED["media"][0]
    check("nama berkas terbawa", b"PO_FRM-PO-IT-2026-IX-1.pdf" in blob)
    check("isi berkas terbawa", b"%PDF-1.4 dummy" in blob)


# ── 5. Sesi ──────────────────────────────────────────────────────────────────

print("\nsesi")

first = bridge.ROUTER.hermes_session("628123")
check("nama sesi stabil untuk nomor yang sama", bridge.ROUTER.hermes_session("628123") == first)
after_reset = bridge.ROUTER.reset("628123")
check("reset menghasilkan sesi baru", after_reset != first, f"{first} -> {after_reset}")
check("sesi tersimpan ke disk", json.loads((WORK / "sessions.json").read_text())["628123"] == after_reset)
check("karakter aneh dibuang dari nama sesi",
      bridge.ROUTER.hermes_session("62812/../x") == "wa-62812x", bridge.ROUTER.hermes_session("62812/../x"))

print()
if FAILURES:
    print(f"{len(FAILURES)} gagal: {', '.join(FAILURES)}")
    sys.exit(1)
print("selfcheck prpo-bridge OK")
