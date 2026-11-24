#!/usr/bin/env python3
"""Render a small spinning-vinyl GIF used as the /now embed thumbnail.

Build script on purpose: regenerate the asset (size, colours, frames) without
touching a binary. Run: .venv/bin/python tools/make_vinyl.py
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "vinyl.gif")
SIZE = 128
SS = 4              # supersample
S = SIZE * SS
FRAMES = 16
DELAY_MS = 45
ACCENT = (88, 101, 242, 255)      # blurple label
INK = (16, 17, 20, 255)
RIM = (104, 110, 128, 255)          # visible edge: a black disc vanishes on dark UI
GROOVE_A = (44, 46, 55, 255)
GROOVE_B = (96, 102, 120, 255)


def artwork() -> Image.Image:
    """Rotating part: body, grooves, label, markers, spindle hole."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = S / 2
    r_out = 0.47 * S
    d.ellipse([c - r_out, c - r_out, c + r_out, c + r_out], fill=INK)

    # fewer, bolder grooves than a real record: they must survive 80 px
    r, i = 0.43 * S, 0
    while r > 0.215 * S:
        d.ellipse([c - r, c - r, c + r, c + r],
                  outline=GROOVE_A if i % 2 else GROOVE_B, width=int(0.007 * S))
        r -= 0.055 * S
        i += 1

    d.ellipse([c - r_out + 0.008 * S, c - r_out + 0.008 * S,
               c + r_out - 0.008 * S, c + r_out - 0.008 * S],
              outline=RIM, width=int(0.009 * S))

    lab = 0.19 * S
    d.ellipse([c - lab, c - lab, c + lab, c + lab], fill=ACCENT)
    d.ellipse([c - lab * 0.80, c - lab * 0.80, c + lab * 0.80, c + lab * 0.80],
              outline=(255, 255, 255, 48), width=int(0.007 * S))

    # motion cues: a light wedge in the label, two bold dots and a radial tick
    # (a symmetric disc reads as static at 80 px)
    d.pieslice([c - lab * 0.80, c - lab * 0.80, c + lab * 0.80, c + lab * 0.80],
               start=-100, end=-45, fill=(255, 255, 255, 235))
    for ang, rad, size in ((0, 0.150 * S, 0.026 * S), (200, 0.145 * S, 0.016 * S)):
        a = math.radians(ang)
        x, y = c + rad * math.cos(a), c + rad * math.sin(a)
        d.ellipse([x - size, y - size, x + size, y + size], fill=(255, 255, 255, 235))
    a = math.radians(210)
    d.line([(c + 0.30 * S * math.cos(a), c + 0.30 * S * math.sin(a)),
            (c + 0.445 * S * math.cos(a), c + 0.445 * S * math.sin(a))],
           fill=ACCENT, width=int(0.022 * S))

    h = 0.021 * S
    d.ellipse([c - h, c - h, c + h, c + h], fill=INK)
    return img


def gloss() -> Image.Image:
    """Static specular highlight — light does not spin with the record."""
    g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    c = S / 2
    d.arc([c - 0.44 * S, c - 0.44 * S, c + 0.44 * S, c + 0.44 * S], 205, 255,
          fill=(255, 255, 255, 62), width=int(0.015 * S))
    d.arc([c - 0.30 * S, c - 0.30 * S, c + 0.30 * S, c + 0.30 * S], 210, 245,
          fill=(255, 255, 255, 36), width=int(0.010 * S))
    return g


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    base = artwork()
    shine = gloss()
    frames = []
    for i in range(FRAMES):
        f = base.rotate(-i * 360 / FRAMES, resample=Image.BICUBIC, center=(S / 2, S / 2))
        f.alpha_composite(shine)
        f = f.resize((SIZE, SIZE), Image.LANCZOS)

        flat = Image.new("RGB", (SIZE, SIZE), INK[:3])
        flat.paste(f, (0, 0), f)
        pal = flat.quantize(colors=15, method=Image.MEDIANCUT, dither=Image.Dither.NONE)
        alpha = f.split()[3].point(lambda a: 255 if a < 40 else 0)
        pal.paste(15, mask=alpha)
        frames.append(pal)

    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=DELAY_MS,
                   loop=0, disposal=2, transparency=15, optimize=True)
    print(f"wrote {OUT} ({os.path.getsize(OUT) / 1024:.1f} KB, {FRAMES} frames)")


if __name__ == "__main__":
    main()