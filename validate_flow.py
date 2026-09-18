#!/usr/bin/env python3
"""
Periksa canvas Flow Builder terhadap aturan yang benar-benar dipakai
flow-executor.js. Jalankan: python3 validate_flow.py flow-psg-help-center.json

Yang diperiksa, semuanya diturunkan dari kode executor:

1. Semua node terjangkau dari Start (node yatim = kerja yang tidak pernah jalan).
2. Node Tanya Jawab hanya boleh punya SATU edge keluar — saat percakapan
   dilanjutkan, executor mengambil getNextNodeIds(...)[0] tanpa melihat label.
3. Node dua-port (condition, api_call, operating_hours) wajib punya port 'true'
   DAN 'false', atau satu edge polos. Port yang tidak terpasang = flow berhenti
   diam-diam di tengah percakapan.
4. Node Switch: setiap case punya edge berlabel sama persis, plus 'default'.
5. Setiap siklus WAJIB melewati node Tanya Jawab. Executor punya visited-set per
   giliran: siklus tanpa titik tunggu terputus di putaran kedua, dan gejalanya
   "flow berhenti tanpa pesan" — bukan error, bukan log yang mencolok.
6. Edge tidak menunjuk node yang tidak ada.
7. URL node API Call https dan tidak berisi placeholder yang belum diganti.
"""

import json
import re
import sys
from pathlib import Path

TWO_PORT = {"condition", "condition_node", "api_call", "operating_hours"}
WAIT = {"ask_question", "askQuestion"}

problems: list[str] = []
notes: list[str] = []


def main(path: Path) -> int:
    canvas = json.loads(path.read_text())
    nodes = {n["id"]: n for n in canvas.get("nodes", [])}
    edges = canvas.get("edges", [])

    out: dict[str, list[dict]] = {nid: [] for nid in nodes}
    for e in edges:
        src, dst = e.get("from") or e.get("source"), e.get("to") or e.get("target")
        if src not in nodes:
            problems.append(f"edge dari node yang tidak ada: {src}")
            continue
        if dst not in nodes:
            problems.append(f"edge ke node yang tidak ada: {dst} (dari {src})")
            continue
        out[src].append({"to": dst, "label": (e.get("label") or "").strip()})

    start = next((n for n in nodes.values() if n["type"] in ("start", "trigger")), None)
    if not start:
        problems.append("tidak ada node Start")
        return report()

    # 1. keterjangkauan
    seen, stack = set(), [start["id"]]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(e["to"] for e in out[nid])
    for nid in nodes:
        if nid not in seen:
            problems.append(f"node tidak terjangkau dari Start: {nid} ({nodes[nid]['type']})")

    # 2-4. bentuk port per jenis node
    for nid, n in nodes.items():
        ports = out[nid]
        labels = {p["label"].lower() for p in ports}

        if n["type"] in WAIT and len(ports) > 1:
            problems.append(
                f"{nid}: node Tanya Jawab punya {len(ports)} edge keluar — executor "
                f"hanya memakai yang pertama, sisanya mati"
            )

        if n["type"] in TWO_PORT and ports:
            if not {"true", "false"} <= labels and any(p["label"] for p in ports):
                missing = {"true", "false"} - labels
                problems.append(f"{nid} ({n['type']}): port {'/'.join(sorted(missing))} tidak terpasang")

        if n["type"] == "switch":
            for case in n.get("config", {}).get("cases", []):
                want = (case.get("value") or "").strip().lower()
                if want and want not in labels:
                    problems.append(f"{nid}: case '{case.get('value')}' tidak punya edge")
            if "default" not in labels:
                notes.append(f"{nid}: tidak ada port 'default' — jawaban di luar case menghentikan flow")

        if n["type"] == "api_call":
            url = n.get("config", {}).get("url", "")
            if not url.startswith("https://"):
                problems.append(f"{nid}: URL API Call harus https (sekarang: {url or 'kosong'})")
            if re.search(r"GANTI|CHANGE|TODO|example\.com", url, re.I):
                problems.append(f"{nid}: URL masih berisi placeholder — {url}")
            if re.search(r"localhost|127\.0\.0\.1|192\.168\.|10\.\d+\.", url):
                problems.append(f"{nid}: alamat lokal diblokir safeFetch — {url}")

    # 5. siklus tanpa titik tunggu
    for cycle in find_cycles(out, nodes):
        if not any(nodes[nid]["type"] in WAIT for nid in cycle):
            problems.append("siklus tanpa node Tanya Jawab: " + " → ".join(cycle + [cycle[0]]))

    return report()


def find_cycles(out, nodes) -> list[list[str]]:
    """DFS sederhana; canvas flow berukuran puluhan node, bukan ribuan."""
    found, path, on_path, seen = [], [], set(), set()

    def walk(nid: str) -> None:
        path.append(nid)
        on_path.add(nid)
        for e in out[nid]:
            nxt = e["to"]
            if nxt in on_path:
                found.append(path[path.index(nxt):].copy())
            elif nxt not in seen:
                walk(nxt)
        on_path.discard(nid)
        path.pop()
        seen.add(nid)

    for nid in nodes:
        if nid not in seen:
            walk(nid)
    return found


def report() -> int:
    for note in notes:
        print(f"  catatan  {note}")
    if problems:
        for p in problems:
            print(f"  MASALAH  {p}")
        print(f"\n{len(problems)} masalah ditemukan")
        return 1
    print("canvas lolos semua pemeriksaan")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "flow-psg-help-center.json")
    sys.exit(main(target))
