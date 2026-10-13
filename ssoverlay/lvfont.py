#!/usr/bin/env python3
"""Decode LVGL v7 lv_font_fmt_txt fonts compiled into the A810Pro main_app.

Only reads the analysed ELF copy. Font struct layout in this build
(lv_coord_t is 32-bit): get_glyph_dsc, get_glyph_bitmap, line_height,
base_line, {subpx, underline_pos, underline_thickness}, dsc.
"""
import os
import struct
import zlib
from pathlib import Path

# A copy of main_app (MD5 40c91047536e78e80572363fe2ee8a4b), e.g. pulled from
# /proc/<pid>/exe. It is vendor code and is not shipped with this repo.
ELF = Path(os.environ.get("MAIN_APP_ELF", Path(__file__).resolve().parents[3] /
                          "captures/2026-10-12-readonly/proc__self__exe"))
LOAD_BIAS = 0x10000  # single PT_LOAD: VA = file offset + 0x10000

# Runtime lv_plugin font table (read from device memory at *0x1b36c50 + 4).
FONTS = {
    "small4": 0x1a1df18,   # lh 18, 4bpp
    "ui_a": 0x19fe9fc,     # lh 39, 2bpp, CJK (large set)
    "ui_zh": 0x19fea6c,    # lh 39, 2bpp, simplified Chinese subset
    "ui_lat": 0x19fea34,   # lh 38, 2bpp, Latin/Thai/Vietnamese
    "ui_ar": 0x19fe98c,    # lh 46, 2bpp, Arabic
    "ui_b": 0x19fe9c4,     # lh 39, 2bpp, CJK
}

OPA = {1: [0, 255], 2: [0, 85, 170, 255], 4: [i * 17 for i in range(16)]}


class Font:
    def __init__(self, va, elf=ELF):
        self.b = Path(elf).read_bytes()
        f = self._u("<6I", va)
        self.line_height, self.base_line = struct.unpack("<ii", struct.pack("<II", f[2], f[3]))
        d = self._u("<4IHHI", f[5])
        self.bitmap, self.glyph_dsc, cmaps, kern, self.kern_scale, bits, _ = d
        n = bits & 0x1FF
        self.bpp = (bits >> 9) & 0xF
        self.kern_classes = (bits >> 13) & 1
        if (bits >> 14) & 3:
            raise NotImplementedError("compressed bitmaps")
        self.cmaps = [self._u("<IHHIIHB", cmaps + 20 * i) for i in range(n)]
        self.kern = kern

    def _u(self, fmt, va):
        return struct.unpack_from(fmt, self.b, va - LOAD_BIAS)

    def gid(self, ch):
        c = ord(ch)
        for start, length, gstart, ulist, olist, llen, typ in self.cmaps:
            rc = c - start
            if not 0 <= rc < length:
                continue
            if typ == 2:
                return gstart + rc
            if typ == 0:
                return gstart + self._u("B", olist + rc)[0]
            deltas = self._u(f"<{llen}H", ulist)
            if rc in deltas:
                i = deltas.index(rc)
                return gstart + (i if typ == 3 else self._u("<H", olist + 2 * i)[0])
        return 0

    def glyph(self, ch):
        g = self.gid(ch)
        if not g:
            return None
        w0, bw, bh, ox, oy = self._u("<IBBbb", self.glyph_dsc + 8 * g)
        idx, adv = w0 & 0xFFFFF, w0 >> 20
        n = bw * bh
        raw = self.b[self.bitmap + idx - LOAD_BIAS:][: (n * self.bpp + 7) // 8 + 1]
        opa = OPA[self.bpp]
        px = []
        for i in range(n):
            bit = i * self.bpp
            v = (raw[bit >> 3] >> (8 - self.bpp - (bit & 7))) & ((1 << self.bpp) - 1)
            px.append(opa[v])
        return {"adv16": adv, "w": bw, "h": bh, "ox": ox, "oy": oy, "a": px, "gid": g}

    def kerning(self, a, b):
        if not self.kern or not self.kern_classes:
            return 0
        ga, gb = self.gid(a), self.gid(b)
        if not ga or not gb:
            return 0
        vals, lmap, rmap, lcnt, rcnt = self._u("<3IBB", self.kern)
        lc = self._u("B", lmap + ga)[0]
        rc = self._u("B", rmap + gb)[0]
        if not lc or not rc:
            return 0
        v = self._u("b", vals + (lc - 1) * rcnt + (rc - 1))[0]
        return (v * self.kern_scale) >> 4  # 1/16 px


def render(font, text, scale=1):
    """Return (w, h, alpha list) for one line, LVGL-style placement."""
    x16 = 0
    placed = []
    prev = None
    for ch in text:
        if prev is not None:
            x16 += font.kerning(prev, ch)
        g = font.glyph(ch)
        if g:
            placed.append((x16, g))
            x16 += g["adv16"]
        prev = ch
    w = (x16 + 15) // 16 + 2
    h = font.line_height
    img = [0] * (w * h)
    for gx16, g in placed:
        x0 = (gx16 + 8) // 16 + g["ox"]
        y0 = (font.line_height - font.base_line) - g["h"] - g["oy"]
        for yy in range(g["h"]):
            for xx in range(g["w"]):
                a = g["a"][yy * g["w"] + xx]
                X, Y = x0 + xx, y0 + yy
                if a and 0 <= X < w and 0 <= Y < h:
                    img[Y * w + X] = max(img[Y * w + X], a)
    return w, h, img


def write_png(path, w, h, rgb_rows):
    def ch(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + bytes(r) for r in rgb_rows)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1])
    text = sys.argv[2] if len(sys.argv) > 2 else "km/h 37 mph N 32.24004° W 110.95007° 航向 179° 12:34"
    rows = []
    for name, va in FONTS.items():
        f = Font(va)
        w, h, img = render(f, f"{name}: {text}")
        for y in range(h):
            rows.append([v for x in range(900) for v in ((img[y * w + x],) * 3 if x < w else (0, 0, 0))])
    write_png(out, 900, len(rows), rows)
    print(out)
