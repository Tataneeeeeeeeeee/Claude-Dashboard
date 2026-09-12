#!/usr/bin/env python3
"""Generate the application icon set from code.

Run once (or after changing the design) with the project virtualenv:

    .venv/bin/python tools/make_icon.py

Writes ``assets/icon.png``, ``assets/icon.ico`` and ``assets/icon.icns``.
Drawing the icon rather than shipping a binary blob keeps the repository
free of opaque assets and lets the design be tweaked in one place.

The mark is a rounded terminal-style tile with an angle-bracket prompt and a
rising bar chart, which reads at 16 px as well as at 1024 px.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"

BACKGROUND = (28, 30, 38, 255)
ACCENT = (217, 119, 87, 255)       # the warm Claude terracotta
INK = (240, 238, 234, 255)
DIM = (120, 126, 140, 255)


def draw_icon(size: int) -> Image.Image:
    """Render the icon at *size* x *size* pixels.

    Everything is expressed as a fraction of *size* so the shape stays
    balanced at every resolution.
    """
    scale = 4                                   # supersample, then downscale
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    radius = int(canvas * 0.22)
    draw.rounded_rectangle([0, 0, canvas - 1, canvas - 1], radius=radius, fill=BACKGROUND)

    # A hairline inner border lifts the tile off dark desktop backgrounds.
    draw.rounded_rectangle(
        [canvas * 0.025, canvas * 0.025, canvas * 0.975, canvas * 0.975],
        radius=int(radius * 0.9),
        outline=(255, 255, 255, 18),
        width=max(1, int(canvas * 0.005)),
    )

    # Prompt chevron and underscore occupy the top half only, so they never
    # collide with the bars below.
    stroke = max(2, int(canvas * 0.055))
    draw.line(
        [(canvas * 0.23, canvas * 0.24), (canvas * 0.37, canvas * 0.37),
         (canvas * 0.23, canvas * 0.50)],
        fill=ACCENT, width=stroke, joint="curve",
    )
    draw.line(
        [(canvas * 0.47, canvas * 0.50), (canvas * 0.72, canvas * 0.50)],
        fill=DIM, width=stroke,
    )

    # Bar chart along the bottom, the "dashboard" half of the idea.
    bars = [(0.24, 0.11), (0.41, 0.19), (0.58, 0.15), (0.75, 0.25)]
    base = canvas * 0.80
    width = canvas * 0.105
    for index, (left, height) in enumerate(bars):
        x0 = canvas * left - width / 2
        y0 = base - canvas * height
        colour = ACCENT if index == len(bars) - 1 else INK
        draw.rounded_rectangle(
            [x0, y0, x0 + width, base],
            radius=int(width * 0.3),
            fill=colour,
        )

    return image.resize((size, size), Image.LANCZOS)


def write_icns(path: Path, images: dict[int, Image.Image]) -> None:
    """Write a minimal ICNS container.

    Pillow can only save ICNS on macOS, so the container is assembled by
    hand: an ``icns`` header followed by one PNG-payload chunk per size.
    """
    types = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07",
             256: b"ic08", 512: b"ic09", 1024: b"ic10"}
    chunks = []
    for size, code in types.items():
        image = images.get(size)
        if image is None:
            continue
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        payload = buffer.getvalue()
        chunks.append(code + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(chunks)
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main() -> None:
    """Render every size and write the three icon files."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256, 512, 1024]
    images = {size: draw_icon(size) for size in sizes}

    images[512].save(ASSETS / "icon.png")
    images[1024].save(ASSETS / "icon@1024.png")
    # Windows .ico: Pillow writes every requested size into one container.
    images[256].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
    )
    write_icns(ASSETS / "icon.icns", images)

    for name in ("icon.png", "icon.ico", "icon.icns"):
        target = ASSETS / name
        print(f"{name:12s} {target.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
