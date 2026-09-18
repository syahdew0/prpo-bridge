#!/usr/bin/env python3
"""
Pasang canvas hasil build_flow.py ke flow yang sudah ada di Satuchat, lewat
PATCH /api/v1/organizations/:org/flows/:flow.

MENIMPA seluruh isi canvas flow tujuan. Tanpa --yes hanya menampilkan ringkasan
dan tidak mengirim apa pun. Setelah berhasil, canvas masih Draft — tekan
Publish di Flow Builder supaya versinya dipakai percakapan sungguhan.

Token diambil dari devtools browser (Application → Local Storage → accessToken,
atau header Authorization pada request apa pun dari dashboard).

Contoh:
  python3 apply_flow.py --base https://api.satuchat.example/api/v1 \
      --org <uuid-org> --flow <uuid-flow> --token <jwt> --yes
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="base API, termasuk /api/v1")
    ap.add_argument("--org", required=True)
    ap.add_argument("--flow", required=True)
    ap.add_argument("--token", required=True, help="JWT dashboard")
    ap.add_argument("--file", default="flow-psg-help-center.json")
    ap.add_argument("--yes", action="store_true", help="benar-benar kirim")
    args = ap.parse_args()

    canvas = json.loads(Path(args.file).read_text())
    nodes, edges = canvas.get("nodes", []), canvas.get("edges", [])
    print(f"{args.file}: {len(nodes)} node, {len(edges)} edge")
    for n in nodes:
        print(f"  - {n['id']:<16} {n['type']:<14} {n.get('label','')}")

    if not args.yes:
        print("\nDry-run. Tambahkan --yes untuk MENIMPA canvas flow tujuan.")
        return 0

    url = f"{args.base.rstrip('/')}/organizations/{args.org}/flows/{args.flow}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"canvasData": canvas}).encode(),
        method="PATCH",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {args.token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            print(f"\nOK ({res.status}). Canvas tersimpan sebagai Draft — tekan Publish di Flow Builder.")
            return 0
    except urllib.error.HTTPError as err:
        print(f"\nGagal ({err.code}): {err.read().decode(errors='replace')[:400]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
