#!/usr/bin/env python3
"""Draw the Helix OS boot animation: a glossy blue double helix lying on its side, rotating, plus three dots whose
focus moves 1 -> 2 -> 3 -> 1. Output: plymouth-helix/ (the Plymouth theme's images). Needs Pillow and numpy.

    python3 build/boot-assets/make-helix-splash.py            # (re)generate the PNGs
    python3 build/boot-assets/make-helix-splash.py --preview   # also write preview-sheet.png

The helix is built from shaded spheres (two strands plus the rungs between them) sorted back to front, rendered at 3x
and scaled down for smooth edges. Rotating a double helix by half a turn gives the same picture, so FRAMES frames span
half a turn and the loop is seamless.
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plymouth-helix")
W, H = 640, 200            # one helix frame
SS = 3                     # supersampling
FRAMES = 36
TURNS = 2.7                # twists along the length
LENGTH = 560
RADIUS = 56                # helix radius (pixels, before perspective)
BASE = np.array([52.0, 168.0, 238.0])     # strand blue
RUNG = np.array([150.0, 214.0, 252.0])    # rung light blue
LIGHT = np.array([-0.45, -0.6, 0.66]); LIGHT /= np.linalg.norm(LIGHT)


def sphere(canvas, cx, cy, r, color, shade):
    """Composite one shaded sphere (radius r, in supersampled pixels) onto canvas (H*SS, W*SS, 4 float)."""
    x0, x1 = int(cx - r - 1), int(cx + r + 2); y0, y1 = int(cy - r - 1), int(cy + r + 2)
    x0c, x1c, y0c, y1c = max(x0, 0), min(x1, canvas.shape[1]), max(y0, 0), min(y1, canvas.shape[0])
    if x1c <= x0c or y1c <= y0c:
        return
    ys, xs = np.mgrid[y0c:y1c, x0c:x1c]
    dx, dy = (xs + 0.5 - cx) / r, (ys + 0.5 - cy) / r
    d2 = dx * dx + dy * dy
    inside = d2 <= 1.0
    if not inside.any():
        return
    nz = np.sqrt(np.clip(1 - d2, 0, 1))
    diff = np.clip(dx * LIGHT[0] + dy * LIGHT[1] + nz * LIGHT[2], 0, 1)
    refl = np.clip(2 * (dx * LIGHT[0] + dy * LIGHT[1] + nz * LIGHT[2]) * nz - LIGHT[2], 0, 1)
    spec = refl ** 28
    rim = (1 - nz) ** 2.2
    col = color[None, None, :] * (0.30 + 0.80 * diff[..., None]) * shade
    col = col + 255 * spec[..., None] * 0.95 + np.array([30, 120, 190]) * rim[..., None] * 0.55
    alpha = np.clip((1.0 - np.sqrt(d2)) * r * 1.2, 0, 1) * 0.97          # soft edge
    patch = canvas[y0c:y1c, x0c:x1c]
    a = alpha[..., None]
    patch[..., :3] = patch[..., :3] * (1 - a) + np.clip(col, 0, 255) * a
    patch[..., 3:] = np.clip(patch[..., 3:] + a * (1 - patch[..., 3:]), 0, 1)


def render(phase):
    canvas = np.zeros((H * SS, W * SS, 4), dtype=np.float64)
    prims = []                                # (z, x, y, radius, color, shade)
    n = 900
    for i in range(n + 1):
        t = i / n
        x = (t - 0.5) * LENGTH
        th = 2 * math.pi * TURNS * t + phase
        for k in (0, 1):
            a = th + k * math.pi
            y, z = RADIUS * math.cos(a), RADIUS * math.sin(a)
            prims.append((z, x, y, 7.6 * (1 + 0.20 * z / RADIUS), BASE, 0.62 + 0.38 * (z + RADIUS) / (2 * RADIUS)))
    rungs = 24
    for j in range(rungs + 1):
        t = (j + 0.5) / (rungs + 1)
        x = (t - 0.5) * LENGTH
        th = 2 * math.pi * TURNS * t + phase
        for s in range(1, 61):
            f = s / 61
            a = th
            y = RADIUS * math.cos(a) * (1 - 2 * f); z = RADIUS * math.sin(a) * (1 - 2 * f)
            prims.append((z, x, y, 4.4 * (1 + 0.20 * z / RADIUS), RUNG, 0.58 + 0.42 * (z + RADIUS) / (2 * RADIUS)))
    prims.sort(key=lambda p: p[0])
    for z, x, y, r, color, shade in prims:
        sphere(canvas, (x + W / 2) * SS, (y + H / 2) * SS, r * SS, color, shade)
    img = Image.fromarray(np.clip(canvas * np.array([1, 1, 1, 255]), 0, 255).astype(np.uint8), "RGBA")
    return img.resize((W, H), Image.LANCZOS)


def glow(size, color, strength):
    """Soft radial glow (RGBA)."""
    w, h = size
    ys, xs = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xs - w / 2) / (w / 2)) ** 2 + ((ys - h / 2) / (h / 2)) ** 2)
    a = np.clip(1 - d, 0, 1) ** 2.2 * strength
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = color
    arr[..., 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def dot(size, bright):
    """A dot: bright = in focus (larger, glowing cyan), else small and dim."""
    s = 4
    img = np.zeros((size * s, size * s, 4), dtype=np.float64)
    c = size * s / 2
    r = (size * 0.27 if bright else size * 0.17) * s
    canvas = img
    sphere(canvas, c, c, r, np.array([70.0, 190.0, 250.0]) if bright else np.array([80.0, 125.0, 170.0]), 1.0 if bright else 0.7)
    im = Image.fromarray(np.clip(canvas * np.array([1, 1, 1, 255]), 0, 255).astype(np.uint8), "RGBA")
    if bright:
        halo = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse([c - r * 1.5, c - r * 1.5, c + r * 1.5, c + r * 1.5], fill=(60, 170, 240, 90))
        halo = halo.filter(ImageFilter.GaussianBlur(r * 0.55))
        im = Image.alpha_composite(halo, im)
    return im.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    frames = []
    for k in range(FRAMES):
        im = render(math.pi * k / FRAMES)
        im.save(os.path.join(OUT, f"helix-{k:02d}.png"), optimize=True)
        frames.append(im)
    dot(64, True).save(os.path.join(OUT, "dot-on.png"), optimize=True)
    dot(64, False).save(os.path.join(OUT, "dot-off.png"), optimize=True)
    glow((900, 420), (30, 110, 190), 0.55).save(os.path.join(OUT, "glow.png"), optimize=True)
    if "--preview" in sys.argv:
        sheet = Image.new("RGBA", (W * 2, H * 3 + 70), (20, 20, 20, 255))
        for i, k in enumerate((0, 9, 18, 27, 5, 14)):
            sheet.alpha_composite(frames[k], ((i % 2) * W, (i // 2) * H))
        d1, d0 = dot(64, True), dot(64, False)
        for i, im in enumerate((d1, d0, d0)):
            sheet.alpha_composite(im, (40 + i * 70, H * 3 + 3))
        sheet.convert("RGB").save(os.path.join(OUT, "..", "preview-sheet.png"))
    print(f"wrote {FRAMES} frames to {OUT}")


if __name__ == "__main__" and "--menu" not in sys.argv:
    main()


def menu_background():
    """build/boot-assets/splash.png: the static picture behind the GRUB / ISOLINUX boot menu (1280x720)."""
    from PIL import ImageFont
    W2, H2 = 1280, 720
    bg = Image.new("RGBA", (W2, H2), (20, 20, 20, 255))
    g = glow((1100, 520), (30, 110, 190), 0.5)
    bg.alpha_composite(g, ((W2 - 1100) // 2, 330))
    helix = render(0.0).resize((int(W * 0.85), int(H * 0.85)), Image.LANCZOS)
    hx, hy = (W2 - helix.width) // 2 - 70, 470 - helix.height // 2
    bg.alpha_composite(helix, (hx, hy))
    d1, d0 = dot(64, True), dot(64, False)
    for i, im in enumerate((d1, d0, d0)):
        bg.alpha_composite(im, (hx + helix.width + 10 + i * 54, 470 - 32))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(bg)
    text = "Helix OS"
    tw = d.textlength(text, font=font)
    d.text(((W2 - tw) / 2, 590), text, font=font, fill=(235, 240, 245, 255))
    bg.convert("RGB").save(os.path.join(os.path.dirname(OUT), "splash.png"), optimize=True)
    print("wrote splash.png")


if __name__ == "__main__" and "--menu" in sys.argv:
    menu_background()
