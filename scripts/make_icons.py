#!/usr/bin/env python3
"""Generate Jarvis's application icons.

Pure stdlib (zlib + struct) so the icons are reproducible from source without
an image library in the build environment. Colours come from the design tokens:
deep navy surface, cyan mark.

Usage:  python3 scripts/make_icons.py apps/desktop/src-tauri/icons
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# Matches --surface-sunken / --accent in apps/desktop/src/styles/tokens.css
NAVY = (0x0B, 0x0F, 0x17)
CYAN = (0x38, 0xD6, 0xF0)

# PNG sizes Tauri references, plus the sizes packed into the .ico.
PNG_SIZES = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
    "Square30x30Logo.png": 30,
    "Square44x44Logo.png": 44,
    "Square71x71Logo.png": 71,
    "Square89x89Logo.png": 89,
    "Square107x107Logo.png": 107,
    "Square142x142Logo.png": 142,
    "Square150x150Logo.png": 150,
    "Square284x284Logo.png": 284,
    "Square310x310Logo.png": 310,
    "StoreLogo.png": 50,
}
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _coverage(px: float, py: float, n: int, inside) -> float:
    """2x2 supersampled coverage of `inside` at pixel (px, py). Cheap AA."""
    hits = 0
    for dx in (0.25, 0.75):
        for dy in (0.25, 0.75):
            if inside((px + dx) / n, (py + dy) / n):
                hits += 1
    return hits / 4.0


def _in_rounded_square(u: float, v: float, radius: float = 0.22) -> bool:
    """Squircle-ish rounded square covering the full 0..1 box."""
    cx = min(max(u, radius), 1.0 - radius)
    cy = min(max(v, radius), 1.0 - radius)
    dx, dy = u - cx, v - cy
    return dx * dx + dy * dy <= radius * radius


def _in_j(u: float, v: float) -> bool:
    """A bold block 'J': top bar, descending stem, bottom hook."""
    # Top bar
    if 0.30 <= u <= 0.74 and 0.21 <= v <= 0.33:
        return True
    # Stem
    if 0.56 <= u <= 0.74 and 0.21 <= v <= 0.60:
        return True
    # Bottom hook: lower half of an annulus, open at the top-left.
    cx, cy = 0.45, 0.60
    outer, inner = 0.29, 0.11
    dx, dy = u - cx, v - cy
    d2 = dx * dx + dy * dy
    if v >= cy and inner * inner <= d2 <= outer * outer:
        return True
    return False


def render(n: int) -> bytes:
    """RGBA rows for an n x n icon."""
    rows = bytearray()
    for y in range(n):
        rows.append(0)  # PNG filter type 0 per scanline
        for x in range(n):
            bg = _coverage(x, y, n, _in_rounded_square)
            if bg <= 0.0:
                rows.extend((0, 0, 0, 0))
                continue
            mark = _coverage(x, y, n, _in_j)
            # Composite the cyan mark over navy, then over transparency.
            r = round(NAVY[0] * (1 - mark) + CYAN[0] * mark)
            g = round(NAVY[1] * (1 - mark) + CYAN[1] * mark)
            b = round(NAVY[2] * (1 - mark) + CYAN[2] * mark)
            rows.extend((r, g, b, round(255 * bg)))
    return bytes(rows)


def png(n: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(render(n), 9))
        + chunk(b"IEND", b"")
    )


def ico(sizes=ICO_SIZES) -> bytes:
    """ICO with PNG-compressed entries (supported since Windows Vista)."""
    images = [png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = bytearray(), bytearray()
    for size, data in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 0 means 256
            0 if size >= 256 else size,
            0,  # palette count
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(data),
            offset,
        )
        blobs += data
        offset += len(data)
    return bytes(header + entries + blobs)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/desktop/src-tauri/icons")
    out.mkdir(parents=True, exist_ok=True)
    for name, size in PNG_SIZES.items():
        (out / name).write_bytes(png(size))
    (out / "icon.ico").write_bytes(ico())
    print(f"wrote {len(PNG_SIZES) + 1} icon files to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
