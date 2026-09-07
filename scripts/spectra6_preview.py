#!/usr/bin/env python3
"""Preview how an image will look on the Inky Impression 7.3" (2025, Spectra 6 / E673).

Replicates the quantisation in inky.inky_e673.Inky.set_image so a mock_display_output
render can be checked for colour fidelity before the Pi exists.

Usage:
    python scripts/spectra6_preview.py [input.png] [--saturation 0.5] [-o out.png]

Defaults to mock_display_output/latest.png and writes <input>_spectra6.png alongside it.
"""
import argparse
import os
from PIL import Image

# Copied from inky 2.4.0 inky_e673.py (index 6 is padding, unused by the panel)
DESATURATED_PALETTE = [
    [0, 0, 0], [255, 255, 255], [255, 255, 0], [255, 0, 0], [0, 0, 255], [0, 255, 0], [255, 255, 255]]
SATURATED_PALETTE = [
    [0, 0, 0], [161, 164, 165], [208, 190, 71], [156, 72, 75], [61, 59, 94], [58, 91, 70], [255, 255, 255]]
NAMES = ["black", "white", "yellow", "red", "blue", "green"]


def palette_blend(saturation):
    palette = []
    for i in range(6):
        rs, gs, bs = [c * saturation for c in SATURATED_PALETTE[i]]
        rd, gd, bd = [c * (1.0 - saturation) for c in DESATURATED_PALETTE[i]]
        palette += [int(rs + rd), int(gs + gd), int(bs + bd)]
    return palette


def quantize(image, saturation):
    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette(palette_blend(saturation))
    q = image.convert("RGB").quantize(6, palette=palette_image, dither=Image.Dither.FLOYDSTEINBERG)
    # Display the result using the *saturated* colours, which are closer to what the ink actually looks like
    display_colours = [SATURATED_PALETTE[0], [235, 235, 230]] + SATURATED_PALETTE[2:6]  # paper-white, not driver grey
    q.putpalette(sum(display_colours, []))
    return q.convert("RGB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", default="mock_display_output/latest.png")
    ap.add_argument("--saturation", type=float, default=0.5, help="InkyPi default is 0.5")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    img = Image.open(args.input)
    out = args.output or os.path.splitext(args.input)[0] + "_spectra6.png"
    quantize(img, args.saturation).save(out)

    counts = sorted(img.convert("RGB").quantize(6, palette=(lambda p: (p.putpalette(palette_blend(args.saturation)), p)[1])(Image.new("P", (1, 1))), dither=Image.Dither.FLOYDSTEINBERG).getcolors(), reverse=True)
    total = img.width * img.height
    print(f"wrote {out}")
    for n, idx in counts:
        print(f"  {NAMES[idx]:<7} {100 * n / total:5.1f}%")


if __name__ == "__main__":
    main()
