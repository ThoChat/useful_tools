#!/usr/bin/env python3
"""Add a diagonal text watermark to an image."""

import argparse
from PIL import Image, ImageDraw, ImageFont


def add_watermark(input_path, output_path, text, opacity=10, font_size=60):
    img = Image.open(input_path).convert("RGBA")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    alpha = int(255 * opacity / 100)
    fill = (200, 200, 200, alpha)

    spacing = int(th * 2.5)
    step = int(tw * 1.5 + spacing)

    w, h = img.size
    for y in range(-h, h * 2, step):
        for x in range(-w, w * 2, step):
            draw.text((x + bbox[0], y + bbox[1]), text, font=font, fill=fill)

    rotated = overlay.rotate(
        30, expand=False, resample=Image.BICUBIC, center=(w // 2, h // 2)
    )

    watermarked = Image.alpha_composite(img, rotated)
    watermarked = watermarked.convert("RGB")
    watermarked.save(output_path, quality=95)

    print(f"Watermarked image saved to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add watermark to an image")
    parser.add_argument("input", help="Input image path")
    parser.add_argument("output", help="Output image path")
    parser.add_argument(
        "--text",
        default="All rights reserved",
        help="Watermark text (default: All rights reserved)",
    )
    parser.add_argument(
        "--opacity", type=int, default=30, help="Opacity percentage 0-100 (default: 30)"
    )
    parser.add_argument(
        "--font-size", type=int, default=60, help="Font size (default: 60)"
    )
    args = parser.parse_args()
    add_watermark(args.input, args.output, args.text, args.opacity, args.font_size)
