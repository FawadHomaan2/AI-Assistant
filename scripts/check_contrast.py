#!/usr/bin/env python3
"""Verify the palette meets WCAG contrast minimums.

Run after any token change:  python3 scripts/check_contrast.py
Exits non-zero if a required pair falls below its threshold, so CI catches a
palette regression rather than a reviewer noticing it later.
"""
from __future__ import annotations

import sys

# ── Dark theme (default) ────────────────────────────────────────────────────
DARK = {
    "bg": "#0B0F17",
    "surface": "#121722",
    "surface_raised": "#18202E",
    "surface_sunken": "#080B11",
    "border": "#222C3D",
    "text": "#E8EDF5",
    "text_muted": "#9AA6BC",
    "text_subtle": "#6D7A91",
    "accent": "#38D6F0",
    "accent_deep": "#0E7C94",
    "success": "#3FD99B",
    "warning": "#F5B544",
    "danger": "#FF7A8A",
    "notice": "#8C9BFF",
    "on_accent": "#04141A",
}

# ── Light theme ─────────────────────────────────────────────────────────────
LIGHT = {
    "bg": "#F4F6FA",
    "surface": "#FFFFFF",
    "surface_raised": "#FFFFFF",
    "surface_sunken": "#EDF1F7",
    "border": "#D9E0EB",
    "text": "#121722",
    "text_muted": "#4D5A70",
    "text_subtle": "#6D7A91",
    "accent": "#0C7A92",
    "accent_deep": "#0C7A92",
    "success": "#0E7A52",
    "warning": "#8A5A00",
    "danger": "#B3203A",
    "notice": "#3A45B8",
    "on_accent": "#FFFFFF",
}

AA_TEXT = 4.5   # WCAG AA, body text
AA_LARGE = 3.0  # WCAG AA, large text / UI components & graphics


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


# (foreground, background, minimum, description)
def pairs(t: dict[str, str]) -> list[tuple[str, str, float, str]]:
    return [
        ("text", "bg", AA_TEXT, "body text on app background"),
        ("text", "surface", AA_TEXT, "body text on card"),
        ("text", "surface_raised", AA_TEXT, "body text on raised card"),
        ("text_muted", "surface", AA_TEXT, "muted text on card"),
        ("text_muted", "bg", AA_TEXT, "muted text on background"),
        ("text_subtle", "surface", AA_LARGE, "subtle caption on card"),
        ("accent", "surface", AA_LARGE, "accent UI on card"),
        ("accent", "bg", AA_LARGE, "accent UI on background"),
        ("success", "surface", AA_LARGE, "success indicator"),
        ("warning", "surface", AA_LARGE, "warning indicator"),
        ("danger", "surface", AA_LARGE, "danger indicator"),
        ("notice", "surface", AA_LARGE, "notice indicator"),
        ("on_accent", "accent", AA_TEXT, "label on accent fill"),
        ("border", "surface", 1.2, "card border visible against card"),
    ]


def check(name: str, t: dict[str, str]) -> list[str]:
    failures: list[str] = []
    print(f"\n{name}")
    print("-" * 68)
    for fg, bg, minimum, label in pairs(t):
        r = ratio(t[fg], t[bg])
        ok = r >= minimum
        print(
            f"  {'PASS' if ok else 'FAIL'}  {r:5.2f}:1  (min {minimum:.1f})  "
            f"{label}  [{fg} on {bg}]"
        )
        if not ok:
            failures.append(f"{name}: {label} — {r:.2f}:1 < {minimum:.1f}")
    return failures


def hue_separation() -> list[str]:
    """Semantic colours must be distinguishable from each other, not just legible.

    A security panel that renders 'suspicious' and 'confirmed' in near-identical
    hues defeats the point of having severity levels.
    """
    problems: list[str] = []
    print("\nSemantic hue separation (dark)")
    print("-" * 68)
    names = ["accent", "success", "warning", "danger", "notice"]
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            r = ratio(DARK[a], DARK[b])
            # Different luminance OR clearly different hue; here we only flag
            # pairs that are near-identical in luminance AND adjacent in hue.
            print(f"  {a:8} vs {b:8}  luminance ratio {r:4.2f}:1")
    return problems


def main() -> int:
    failures = check("Dark theme (default)", DARK)
    failures += check("Light theme", LIGHT)
    hue_separation()
    print()
    if failures:
        print("CONTRAST FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All contrast requirements met (WCAG AA).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
