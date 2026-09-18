#!/usr/bin/env python3
"""
Hasilkan canvas Flow Builder "PSG Help Center" — tulis ke flow-psg-help-center.json.

Dibuat lewat skrip, bukan ditulis tangan, supaya yang perlu diganti (URL bridge,
token, daftar nomor, id AI agent) berkumpul di satu tempat di atas sini dan
tidak perlu dicari-cari di tengah JSON.
"""

import json
import os
from pathlib import Path

# ── konfigurasi ──────────────────────────────────────────────────────────────
# Dibaca dari .env (berkas yang sama dengan yang dipakai bridge), BUKAN ditulis
# di sini. Alasannya sederhana: canvas hasil generate memuat BRIDGE_TOKEN di
# dalam URL node API Call, dan repo ini publik. Rahasia yang pernah ter-commit
# harus dianggap bocor selamanya — lebih murah mencegah daripada merotasi.
def _load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

BRIDGE_URL = os.environ.get("BRIDGE_PUBLIC_URL", "https://GANTI-INI.trycloudflare.com")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "GANTI-TOKEN-BRIDGE")
# Daftar nomor dipisah koma: FLOW_NOMOR_DIIZINKAN="+62812...,+62813..."
NOMOR_DIIZINKAN = [n.strip() for n in os.environ.get("FLOW_NOMOR_DIIZINKAN", "").split(",") if n.strip()] \
    or ["+62GANTI-NOMOR-PURCHASING"]
REVIEW_URL = os.environ.get("FLOW_REVIEW_URL", "https://psggroup.co.id/review-dokumen")
AI_AGENT_ID = os.environ.get("FLOW_AI_AGENT_ID", "")   # kosong → AI agent default channel
# ─────────────────────────────────────────────────────────────────────────────

MENU = ["Peraturan Perusahaan", "Tools PR PO", "Review Dokumen"]
KELUAR = ["selesai", "keluar", "menu"]

nodes: list[dict] = []
edges: list[dict] = []


def node(nid: str, ntype: str, label: str, x: int, y: int, **config) -> str:
    nodes.append({"id": nid, "type": ntype, "label": label, "x": x, "y": y, "config": config})
    return nid


def edge(src: str, dst: str, label: str = "") -> None:
    edges.append({"from": src, "to": dst, "label": label})


# ── pintu masuk ──────────────────────────────────────────────────────────────

node("start", "start", "Start", 40, 300, triggers=["menu", "halo", "help", "psg"])

node(
    "whitelist", "condition", "Nomor internal?", 240, 300,
    conditions=[{"val1": "{{contact_phone}}", "operator": "==", "val2": n} for n in NOMOR_DIIZINKAN],
    combinator="OR",
    branches=[],
)
edge("start", "whitelist")

node(
    "tolak", "send_message", "Bukan tim internal", 240, 520,
    message="Maaf, layanan ini hanya tersedia untuk tim internal PSG Group.",
)
edge("whitelist", "tolak", "false")

# ── menu ─────────────────────────────────────────────────────────────────────

node(
    "menu", "ask_question", "Menu utama", 470, 300,
    question=(
        "Halo {{contact_origin_name}} 👋\n"
        "PSG Help Center siap membantu. Pilih layanan:\n\n"
        "1. Peraturan Perusahaan\n"
        "2. Tools PR PO\n"
        "3. Review Dokumen"
    ),
    options=MENU,
    saveToVar="menu_pilihan",
    maxRetry=3,
    persistToContact=False,
)
edge("whitelist", "menu", "true")

node("pilih", "switch", "Arahkan", 720, 300, variable="menu_pilihan",
     cases=[{"operator": "==", "value": m} for m in MENU])
edge("menu", "pilih")

# ── cabang 1 & 3: seperti sekarang ───────────────────────────────────────────

node("ai", "transfer_ai", "Transfer ke AI", 980, 120,
     agentId=AI_AGENT_ID,
     handoverMessage="Saya sambungkan ke asisten peraturan perusahaan ya.")
edge("pilih", "ai", MENU[0])

node("review", "send_message", "Link review", 980, 660,
     message=f"Silakan unggah dokumen untuk direview di:\n{REVIEW_URL}")
edge("pilih", "review", MENU[2])
edge("review", "menu")

edge("pilih", "menu", "default")

# ── cabang 2: Tools PR/PO ────────────────────────────────────────────────────

# Mulai sesi baru di bridge: tiap kali masuk mode ini, riwayat agen dikosongkan
# supaya PR kemarin tidak mencampuri PO hari ini.
node(
    "prpo_start", "api_call", "Mulai sesi PR/PO", 980, 330,
    url=f"{BRIDGE_URL}/wa/start?token={BRIDGE_TOKEN}",
    method="POST",
    body=json.dumps({"session": "{{contact_phone}}", "conversationId": "{{conversation_id}}"}),
    responsePath="",
    responseVariable="prpo_start",
)
edge("pilih", "prpo_start", MENU[1])

node(
    "prpo_intro", "send_message", "Mode aktif", 1220, 330,
    message=(
        "Mode *Tools PR/PO* aktif ✅\n\n"
        "Silakan ceritakan kebutuhanmu — misalnya:\n"
        "_buatkan PO laptop Acer 2 unit @8jt, vendor Online Shopee_\n\n"
        "Asisten akan menanyakan data yang kurang, lalu mengirim berkas "
        "Excel dan PDF-nya ke chat ini.\n"
        "Ketik *selesai* kapan saja untuk kembali ke menu."
    ),
)
edge("prpo_start", "prpo_intro", "true")

# Titik parkir. Pertanyaannya sengaja kosong: yang berbicara ke pelanggan adalah
# asisten PR/PO lewat bridge, bukan flow. Node ini hanya menahan percakapan di
# sini sampai pelanggan menulis lagi.
node("prpo_tunggu", "ask_question", "Tunggu pesan", 1460, 330,
     question="", options=[], saveToVar="", maxRetry=99)
edge("prpo_intro", "prpo_tunggu")

node(
    "prpo_keluar_cek", "condition", "Ketik selesai?", 1700, 330,
    conditions=[{"val1": "{{message_text}}", "operator": "==", "val2": k} for k in KELUAR],
    combinator="OR",
    branches=[],
)
edge("prpo_tunggu", "prpo_keluar_cek")

node(
    "prpo_kirim", "api_call", "Teruskan ke asisten", 1700, 530,
    url=f"{BRIDGE_URL}/wa/message?token={BRIDGE_TOKEN}",
    method="POST",
    body=json.dumps({
        "session": "{{contact_phone}}",
        "conversationId": "{{conversation_id}}",
        "text": "{{message_text}}",
    }),
    responsePath="",
    responseVariable="prpo_kirim",
)
edge("prpo_keluar_cek", "prpo_kirim", "false")
# Balasan datang belakangan lewat bridge → kembali menunggu.
edge("prpo_kirim", "prpo_tunggu", "true")

node(
    "prpo_mati", "send_message", "Asisten tidak aktif", 1460, 730,
    message=(
        "Maaf, asisten PR/PO sedang tidak bisa dihubungi. "
        "Silakan hubungi tim IT untuk mengeceknya. Kembali ke menu utama ya."
    ),
)
edge("prpo_start", "prpo_mati", "false")
edge("prpo_kirim", "prpo_mati", "false")
edge("prpo_mati", "menu")

node("prpo_selesai", "send_message", "Keluar mode", 1940, 150,
     message="Mode Tools PR/PO ditutup. Kembali ke menu utama 👇")
edge("prpo_keluar_cek", "prpo_selesai", "true")
edge("prpo_selesai", "menu")

canvas = {"nodes": nodes, "edges": edges}

out = Path(__file__).with_name("flow-psg-help-center.json")
out.write_text(json.dumps(canvas, indent=2, ensure_ascii=False))
print(f"{out} — {len(nodes)} node, {len(edges)} edge")
