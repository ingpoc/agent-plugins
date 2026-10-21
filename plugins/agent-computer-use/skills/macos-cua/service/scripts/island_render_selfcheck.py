#!/usr/bin/env python3
"""Island render self-check: zoom every island render 2x and lay the crops out on one review sheet.

    CUA_ISLAND_RENDER_DIR=<dir> swift test --filter IslandP7RenderTests
    python3 scripts/island_render_selfcheck.py <dir> <review-sheet.png>
    python3 scripts/island_render_selfcheck.py --window [--glob 'settings-*.png'] <dir> <review-sheet.png>

Each render is cropped to its island (pixels that differ from the corner backdrop, plus 8 px), scaled 2x,
and labelled. A crop that touches the render edge is flagged EDGE (the island may be cut off by the render
canvas).

--window is for full-window renders (Island Settings): the window fills the canvas, so there is no backdrop
and no EDGE check. Each render is cropped to its content pane (right of the 213 pt sidebar; one crop keeps
the sidebar) at its native 2 px/pt, and light / dark pairs that are pixel-identical are shown once.

The deterministic overlap / clipping / inset gate is IslandLayoutGateTests; this sheet is for the eye.
Needs Pillow.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None   # review sheets are large on purpose

PAD, ZOOM, GAP, LABEL, MAX_W, WINDOW_MAX_W = 8, 2, 24, 28, 2600, 3400


def island_crop(img: Image.Image) -> tuple[Image.Image, bool]:
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 24 else 0)
    box = diff.getbbox() or (0, 0, *rgb.size)
    edge = box[0] == 0 or box[1] == 0 or box[2] == rgb.width or box[3] == rgb.height
    box = (max(box[0] - PAD, 0), max(box[1] - PAD, 0), min(box[2] + PAD, rgb.width), min(box[3] + PAD, rgb.height))
    crop = rgb.crop(box)
    return crop.resize((crop.width * ZOOM, crop.height * ZOOM), Image.LANCZOS), edge


SIDEBAR_PX = 214 * 2   # 213 pt sidebar + 1 pt divider, at 2 px/pt


def window_cells(paths: list[Path]) -> list[tuple[str, Image.Image, bool]]:
    def same(a: Path, b: Path) -> bool:
        return b.exists() and ImageChops.difference(Image.open(a).convert("RGB"), Image.open(b).convert("RGB")).getbbox() is None

    cells = []
    for p in paths:
        if p.name.endswith("-dark.png") and same(p, p.with_name(p.name.replace("-dark.png", "-light.png"))):
            continue   # shown once, under its light twin
        img = Image.open(p).convert("RGB")
        label = p.stem
        if p.name.endswith("-light.png") and same(p, p.with_name(p.name.replace("-light.png", "-dark.png"))):
            label = p.stem.removesuffix("-light") + "  (light = dark)"
        crop = img if not cells else img.crop((SIDEBAR_PX, 0, img.width, img.height))
        cells.append((label, crop, False))
    return cells


def main(argv: list[str]) -> int:
    window = "--window" in argv
    pattern = "*.png"
    args = [a for a in argv[1:] if a != "--window"]
    if "--glob" in args:
        i = args.index("--glob"); pattern = args[i + 1]; del args[i:i + 2]
    if len(args) != 2:
        print(__doc__.strip().splitlines()[3].strip(), file=sys.stderr)
        return 2
    src, out = Path(args[0]), Path(args[1])
    if out.exists():
        print(f"{out} exists; move it to the Trash first (never overwrite evidence)", file=sys.stderr)
        return 2
    paths = sorted(src.glob(pattern))
    cells = window_cells(paths) if window else []
    for p in ([] if window else paths):
        crop, edge = island_crop(Image.open(p))
        cells.append((p.stem + ("  EDGE" if edge else ""), crop, edge))
    if not cells:
        print(f"no renders in {src}", file=sys.stderr)
        return 1
    rows, row, x = [], [], 0
    for c in cells:
        if row and x + c[1].width > (WINDOW_MAX_W if window else MAX_W):
            rows.append(row); row, x = [], 0
        row.append(c); x += c[1].width + GAP
    rows.append(row)
    width = max(sum(c[1].width + GAP for c in r) for r in rows) + GAP
    height = sum(max(c[1].height for c in r) + LABEL + GAP for r in rows) + GAP
    sheet = Image.new("RGB", (width, height), (40, 40, 44))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:   # Pillow < 10.1
        font = ImageFont.load_default()
    y = GAP
    for r in rows:
        x = GAP
        for label, crop, edge in r:
            draw.text((x, y + 4), label, font=font, fill=(255, 90, 90) if edge else (230, 230, 230))
            sheet.paste(crop, (x, y + LABEL))
            x += crop.width + GAP
        y += max(c[1].height for c in r) + LABEL + GAP
    sheet.save(out)
    flagged = [c[0] for c in cells if c[2]]
    print(f"review sheet {out}: {len(cells)} renders, {len(flagged)} EDGE" + (": " + ", ".join(flagged) if flagged else ""))
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
