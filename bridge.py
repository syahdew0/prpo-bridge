#!/usr/bin/env python3
"""
prpo-bridge — penghubung antara node API Call di Satuchat Flow Builder dan agen
PR/PO (Hermes + plugin psg-po-pr) yang selama ini dipakai lewat Telegram.

Kenapa ada: node API Call hanya menunggu 10 detik, sedangkan satu giliran agen
butuh puluhan detik dan berakhir dengan berkas .xlsx/.pdf. Jadi alurnya dibalik.

    Satuchat  --POST /wa/message-->  bridge      (dijawab 202 dalam milidetik)
                                       |
                                       v
                          hermes chat -Q -c wa-<nomor>
                                       |
                                       v
    Satuchat  <--POST /api/v1/bridge/{messages,media}--  bridge

Satu antrean per sesi: dua pesan beruntun dari nomor yang sama tidak pernah
menjalankan dua agen sekaligus di atas riwayat yang sama. Antar nomor tetap
berjalan paralel.

Tanpa dependensi di luar pustaka standar Python 3.10+.
Diperiksa oleh: python3 selfcheck.py
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Konfigurasi ──────────────────────────────────────────────────────────────


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"[config] {name} wajib diisi — lihat config.example.env")
    return value or ""


HOST = _env("BRIDGE_HOST", "127.0.0.1")
PORT = int(_env("BRIDGE_PORT", "8787"))

# Token di URL, bukan header: node API Call Satuchat tidak bisa mengirim header
# custom (flow-executor.js hanya menyetel Content-Type).
BRIDGE_TOKEN = _env("BRIDGE_TOKEN", required=True)

SATUCHAT_API_BASE = _env("SATUCHAT_API_BASE", required=True).rstrip("/")
SATUCHAT_BRIDGE_KEY = _env("SATUCHAT_BRIDGE_KEY", required=True)

HERMES_BIN = _env("HERMES_BIN", "hermes")
HERMES_CWD = _env("HERMES_CWD", str(Path.home()))
HERMES_TIMEOUT = int(_env("HERMES_TIMEOUT", "600"))
SESSION_PREFIX = _env("HERMES_SESSION_PREFIX", "wa-")

STATE_FILE = Path(_env("BRIDGE_STATE_FILE", str(Path.home() / ".prpo-bridge" / "sessions.json")))
IDLE_WORKER_SECONDS = int(_env("BRIDGE_IDLE_WORKER_SECONDS", "900"))

# Batas teks satu pesan mengikuti skema validasi di bridge.routes.js (4096).
MAX_TEXT_CHARS = 4000

logging.basicConfig(
    level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("prpo-bridge")


# ── Klien Satuchat ───────────────────────────────────────────────────────────


def _post_json(path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{SATUCHAT_API_BASE}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": SATUCHAT_BRIDGE_KEY},
    )
    return _send(req)


def _post_file(path: str, fields: dict[str, str], file_path: Path) -> tuple[int, dict]:
    """Multipart tanpa `requests` — cukup satu berkas per permintaan."""
    boundary = f"----prpo{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []

    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{file_path.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{SATUCHAT_API_BASE}{path}",
        data=b"".join(parts),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-api-key": SATUCHAT_BRIDGE_KEY,
        },
    )
    return _send(req)


def _send(req: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read() or b"{}"
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"message": raw.decode(errors="replace")[:300]}
    except Exception as err:  # jaringan putus, DNS, timeout
        return 0, {"message": str(err)}


class ConversationGone(Exception):
    """Percakapan ditutup atau sudah diambil agent manusia — bridge berhenti."""


def push_text(conversation_id: str, text: str) -> None:
    for chunk in _chunk(text, MAX_TEXT_CHARS):
        status, body = _post_json("/bridge/messages", {"conversationId": conversation_id, "text": chunk})
        if status == 409:
            raise ConversationGone(body.get("code") or body.get("message") or "409")
        if status != 200:
            log.error("push_text gagal (%s): %s", status, body.get("message"))
            return


def push_file(conversation_id: str, file_path: Path, caption: str = "") -> None:
    status, body = _post_file(
        "/bridge/media",
        {"conversationId": conversation_id, "caption": caption},
        file_path,
    )
    if status == 409:
        raise ConversationGone(body.get("code") or "409")
    if status != 200:
        log.error("push_file gagal (%s) untuk %s: %s", status, file_path.name, body.get("message"))
    else:
        log.info("terkirim: %s", file_path.name)


def _chunk(text: str, size: int) -> list[str]:
    """Potong di batas baris kalau bisa — memotong di tengah kata terlihat rusak."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, rest = [], text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


# ── Pemanggil Hermes ─────────────────────────────────────────────────────────

MEDIA_RE = re.compile(r"MEDIA:\s*(\S+)")
# Jaring pengaman: kadang model menulis path-nya tanpa penanda MEDIA:.
PATH_RE = re.compile(r"(/[^\s'\"<>|]+\.(?:xlsx|pdf))")


@dataclass
class HermesReply:
    text: str
    files: list[Path] = field(default_factory=list)
    error: str | None = None


def run_hermes(session_name: str, message: str) -> HermesReply:
    """
    Satu giliran agen. Teks pengguna masuk lewat stdin (--query-file -), jadi
    tidak pernah ditafsirkan shell: tanda kutip, $(...) dan backtick di pesan
    pelanggan tetap data.
    """
    cmd = [
        HERMES_BIN, "chat",
        "-Q",
        "--query-file", "-",
        "--continue", session_name,
        "--create-if-missing",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=message,
            capture_output=True,
            text=True,
            timeout=HERMES_TIMEOUT,
            cwd=HERMES_CWD,
        )
    except subprocess.TimeoutExpired:
        return HermesReply(text="", error=f"timeout setelah {HERMES_TIMEOUT} detik")
    except FileNotFoundError:
        return HermesReply(text="", error=f"perintah '{HERMES_BIN}' tidak ditemukan")

    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return HermesReply(text="", error=f"hermes keluar dengan kode {proc.returncode}: {detail}")

    text, files = _parse_output(proc.stdout)
    log.info("hermes %s selesai dalam %.1fs — %d berkas", session_name, elapsed, len(files))
    return HermesReply(text=text, files=files)


def _parse_output(stdout: str) -> tuple[str, list[Path]]:
    """
    Pisahkan jawaban untuk pelanggan dari daftar berkasnya.

    Baris "Warning: ..." di awal stdout adalah catatan pemuatan toolset milik
    Hermes, bukan bagian jawaban — kalau ikut terkirim, pelanggan melihat pesan
    teknis yang bukan untuk dia.
    """
    lines = [ln for ln in stdout.splitlines() if not ln.startswith("Warning: ")]
    body = "\n".join(lines)

    seen: list[Path] = []

    def remember(raw: str) -> None:
        path = Path(raw.strip().strip("\"'.,)"))
        if path.is_file() and path not in seen:
            seen.append(path)

    for match in MEDIA_RE.findall(body):
        remember(match)
    if not seen:
        for match in PATH_RE.findall(body):
            remember(match)

    # Buang penanda dan path dari teks yang dibaca pelanggan.
    cleaned = MEDIA_RE.sub("", body)
    for path in seen:
        cleaned = cleaned.replace(str(path), "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip(" \n-–—")

    return cleaned, seen


# ── Antrean per sesi ─────────────────────────────────────────────────────────


@dataclass
class Job:
    conversation_id: str
    session_key: str
    text: str


class SessionRouter:
    """
    Satu antrean + satu thread per sesi. Thread menutup diri sendiri setelah
    menganggur, jadi ribuan nomor tidak berarti ribuan thread hidup.
    """

    def __init__(self) -> None:
        self._queues: dict[str, queue.Queue[Job | None]] = {}
        self._lock = threading.Lock()
        self._names: dict[str, str] = _load_state()

    # Nama sesi Hermes untuk satu kunci sesi (biasanya nomor WA).
    def hermes_session(self, session_key: str) -> str:
        with self._lock:
            name = self._names.get(session_key)
            if not name:
                name = f"{SESSION_PREFIX}{_slug(session_key)}"
                self._names[session_key] = name
                _save_state(self._names)
            return name

    def reset(self, session_key: str) -> str:
        """Mulai riwayat baru — dipanggil saat pelanggan masuk ke mode PR/PO."""
        with self._lock:
            name = f"{SESSION_PREFIX}{_slug(session_key)}-{int(time.time())}"
            self._names[session_key] = name
            _save_state(self._names)
        log.info("sesi %s direset ke %s", session_key, name)
        return name

    def submit(self, job: Job) -> None:
        with self._lock:
            q = self._queues.get(job.session_key)
            if q is None:
                q = queue.Queue()
                self._queues[job.session_key] = q
                threading.Thread(target=self._worker, args=(job.session_key, q), daemon=True).start()
            q.put(job)

    def _worker(self, session_key: str, q: "queue.Queue[Job | None]") -> None:
        while True:
            try:
                job = q.get(timeout=IDLE_WORKER_SECONDS)
            except queue.Empty:
                with self._lock:
                    # Balapan: pesan bisa masuk antara timeout dan lock.
                    if q.empty():
                        self._queues.pop(session_key, None)
                        return
                continue
            if job is None:
                return
            try:
                self._handle(job)
            except ConversationGone as gone:
                log.info("percakapan %s tidak menerima pesan lagi (%s) — antrean dibuang",
                         job.conversation_id, gone)
                _drain(q)
            except Exception:
                log.exception("giliran gagal untuk %s", session_key)
            finally:
                q.task_done()

    def _handle(self, job: Job) -> None:
        session_name = self.hermes_session(job.session_key)
        reply = run_hermes(session_name, job.text)

        if reply.error:
            log.error("hermes error (%s): %s", session_name, reply.error)
            push_text(
                job.conversation_id,
                "Maaf, asisten PR/PO sedang tidak bisa dihubungi. Coba ulangi sebentar lagi, "
                "atau ketik *selesai* untuk kembali ke menu.",
            )
            return

        if reply.text:
            push_text(job.conversation_id, reply.text)
        elif not reply.files:
            push_text(job.conversation_id, "(tidak ada jawaban dari asisten PR/PO)")

        for path in reply.files:
            push_file(job.conversation_id, path)


def _drain(q: "queue.Queue[Job | None]") -> None:
    while True:
        try:
            q.get_nowait()
            q.task_done()
        except queue.Empty:
            return


def _slug(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:40] or "anon"


def _load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(names: dict[str, str]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(names, indent=2))
    except Exception as err:
        log.warning("state tidak tersimpan: %s", err)


ROUTER = SessionRouter()


# ── HTTP ─────────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "prpo-bridge/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------------

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, parsed) -> bool:
        given = (parse_qs(parsed.query).get("token") or [""])[0]
        return hmac.compare_digest(given, BRIDGE_TOKEN)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorized(parsed):
            return self._reply(401, {"error": "token tidak valid"})
        if parsed.path == "/health":
            return self._reply(200, {"ok": True})
        self._reply(404, {"error": "rute tidak dikenal"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorized(parsed):
            log.warning("permintaan ditolak: token salah (%s)", self.address_string())
            return self._reply(401, {"error": "token tidak valid"})

        payload = self._json_body()
        session_key = str(payload.get("session") or payload.get("phone") or "").strip()
        conversation_id = str(payload.get("conversationId") or "").strip()

        if parsed.path == "/wa/start":
            if not session_key:
                return self._reply(400, {"error": "session wajib diisi"})
            ROUTER.reset(session_key)
            return self._reply(200, {"status": "reset"})

        if parsed.path == "/wa/message":
            text = str(payload.get("text") or "").strip()
            if not (session_key and conversation_id and text):
                return self._reply(400, {"error": "session, conversationId, dan text wajib diisi"})
            ROUTER.submit(Job(conversation_id=conversation_id, session_key=session_key, text=text))
            # 202 sebelum agen jalan: inilah yang membuat node API Call tidak
            # pernah menyentuh batas 10 detiknya.
            return self._reply(202, {"status": "queued"})

        self._reply(404, {"error": "rute tidak dikenal"})


def main() -> None:
    status, body = _send(
        urllib.request.Request(
            f"{SATUCHAT_API_BASE}/bridge/health",
            headers={"x-api-key": SATUCHAT_BRIDGE_KEY},
        )
    )
    if status != 200:
        log.warning("Satuchat /bridge/health menjawab %s (%s) — bridge tetap jalan, "
                    "tapi balasan tidak akan sampai sebelum ini benar",
                    status, body.get("message"))
    else:
        log.info("Satuchat bridge API terjangkau di %s", SATUCHAT_API_BASE)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    log.info("prpo-bridge mendengarkan di http://%s:%d", HOST, PORT)
    httpd.serve_forever()
    log.info("berhenti")


if __name__ == "__main__":
    main()
