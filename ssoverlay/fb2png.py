#!/usr/bin/env python3
"""Convert an A810Pro fb0 dump (480x640 BGRA, rotated) to a 640x480 PNG as seen on the panel."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lvfont import write_png  # noqa: E402

W, H = 480, 640
raw = Path(sys.argv[1]).read_bytes()
rows = []
for ny in range(W):
    row = bytearray()
    for nx in range(H):
        p = ((H - 1 - nx) * W + ny) * 4
        row += bytes((raw[p + 2], raw[p + 1], raw[p]))
    rows.append(row)
write_png(sys.argv[2], H, W, rows)
