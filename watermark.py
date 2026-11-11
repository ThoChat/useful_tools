#!/usr/bin/env python3
r"""Add a text watermark to an image, with a GUI or headless.

By default the tool opens a small window where you can open an image, type
the watermark text, see a live preview, and choose:

* the font (sans, serif, mono or any font file) and the text size, in
  pixels or as a percentage of the image's shorter side,
* the text colour, an optional contrasting outline, and the opacity,
* the layout (a tiled diagonal pattern, one centred copy, or a signature
  in the bottom-right corner), the angle and the spacing between copies,
* the output format (PNG, JPG, WEBP, TIFF or BMP), the JPG/WEBP quality,
  and the output file name and folder.

With ``--headless`` the same options are given on the command line and the
file is written straight away, without opening any window (useful in
scripts or over SSH). Without ``--headless``, command-line options pre-fill
the GUI.

The watermark is drawn at the image's full resolution. Photos are turned
upright according to their EXIF orientation first; the colour profile and
DPI are kept, other metadata (camera, GPS...) is not copied. Transparent
images stay transparent in formats that support it. The original image is
never overwritten.

Usage:
  uv run watermark.py
  uv run watermark.py photo.jpg
  uv run watermark.py --headless photo.jpg          # -> photo_watermarked.jpg
  uv run watermark.py --headless photo.jpg out.png --text "© Jane Doe"
  uv run watermark.py --headless photo.jpg --layout corner --angle 0 \
      --font-size 4% --color white --opacity 70 --outline
"""

import argparse
import math
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlparse

from PIL import (
    Image,
    ImageColor,
    ImageDraw,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

# The GUI needs tkinter plus customtkinter (modern widgets, light/dark mode).
# Importing them tolerantly keeps headless mode usable on machines where
# Python was built without Tk.
try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox

    import customtkinter as ctk
    from PIL import ImageTk
except ImportError:  # pragma: no cover - depends on the Python build
    tk = ctk = ImageTk = None


class OutputFormat(NamedTuple):
    """Capabilities of one output file format.

    Attributes:
        pil_name: Pillow format name used to save the image.
        supports_alpha: Whether the format can store transparency.
        lossy: Whether the format takes a quality setting.
    """

    pil_name: str
    supports_alpha: bool
    lossy: bool


# Supported output formats, keyed by their canonical file extension.
FORMATS: dict[str, OutputFormat] = {
    "png": OutputFormat("PNG", supports_alpha=True, lossy=False),
    "jpg": OutputFormat("JPEG", supports_alpha=False, lossy=True),
    "webp": OutputFormat("WEBP", supports_alpha=True, lossy=True),
    "tiff": OutputFormat("TIFF", supports_alpha=True, lossy=False),
    "bmp": OutputFormat("BMP", supports_alpha=False, lossy=False),
}

# Alternative spellings accepted for formats / file extensions.
FORMAT_ALIASES = {"jpeg": "jpg", "tif": "tiff"}

# Pillow's name for an opened file's format -> output format that keeps it.
# (iPhone photos are often reported as MPO, a multi-picture JPEG.)
SOURCE_FORMATS = {
    "PNG": "png",
    "JPEG": "jpg",
    "MPO": "jpg",
    "WEBP": "webp",
    "TIFF": "tiff",
    "BMP": "bmp",
}

# Where the text goes: a repeating diagonal pattern over the whole image,
# a single copy in the middle, or a signature in the bottom-right corner.
LAYOUTS = ("tiled", "centre", "corner")
LAYOUT_ALIASES = {"center": "centre", "tile": "tiled"}

# Font files tried, in order, for each generic family. Pillow searches the
# system font folders for bare file names (macOS, Windows, then Linux names).
FONT_FAMILIES: dict[str, tuple[str, ...]] = {
    "sans": (
        "Helvetica.ttc",
        "Arial.ttf",
        "arial.ttf",
        "DejaVuSans.ttf",
        "LiberationSans-Regular.ttf",
    ),
    "serif": (
        "Times.ttc",
        "Times New Roman.ttf",
        "times.ttf",
        "DejaVuSerif.ttf",
        "LiberationSerif-Regular.ttf",
    ),
    "mono": (
        "Menlo.ttc",
        "Courier New.ttf",
        "consola.ttf",
        "cour.ttf",
        "DejaVuSansMono.ttf",
        "LiberationMono-Regular.ttf",
    ),
}

DEFAULT_TEXT = "All rights reserved"
DEFAULT_FONT = "sans"
DEFAULT_FONT_SIZE = "5%"  # of the image's shorter side
DEFAULT_COLOR = "#c8c8c8"
DEFAULT_OPACITY = 30  # percent
DEFAULT_ANGLE = 30  # degrees, counter-clockwise
DEFAULT_SPACING = 2.0  # gap between tiled copies, in multiples of the text size
DEFAULT_LAYOUT = "tiled"
DEFAULT_QUALITY = 95
DEFAULT_FORMAT = "png"  # used when the input's own format cannot be written
OUTPUT_SUFFIX = "_watermarked"

# Guard rails against nonsense values and runaway rendering times.
MAX_FONT_PX = 5000
MAX_ANGLE = 90
MAX_SPACING = 10.0
MAX_COPIES = 50_000  # tiled copies considered for one image

OUTLINE_RATIO = 0.05  # outline width as a share of the text size
# Smaller text (mostly in the preview) is drawn at this size and scaled
# down: some fonts, e.g. macOS's Helvetica.ttc, cannot be drawn below 8 px.
MIN_RENDER_PX = 16
CORNER_MARGIN = 0.03  # corner-layout margin as a share of the shorter side

RGBA = tuple[int, int, int, int]
Size = tuple[int, int]


# --------------------------------------------------------------------------
# Option parsing / validation
# --------------------------------------------------------------------------


def normalize_format(name: str) -> str:
    """Return the canonical format key for a format name or file extension.

    Args:
        name: A format such as ``"png"``, ``".JPEG"`` or ``"tif"``.

    Returns:
        The matching key of :data:`FORMATS` (e.g. ``"jpg"``, ``"tiff"``).

    Raises:
        ValueError: If the format is not supported.
    """
    key = name.strip().lower().lstrip(".")
    key = FORMAT_ALIASES.get(key, key)
    if key not in FORMATS:
        supported = ", ".join(FORMATS)
        raise ValueError(f"Unsupported format '{name}' (choose from: {supported})")
    return key


def format_from_suffix(path: Path) -> str | None:
    """Return the canonical format implied by a path's extension, if any.

    Args:
        path: File path whose suffix is inspected.

    Returns:
        The format key, or ``None`` when the suffix is missing or unknown.
    """
    try:
        return normalize_format(path.suffix) if path.suffix else None
    except ValueError:
        return None


def normalize_layout(name: str) -> str:
    """Return the canonical layout name.

    Args:
        name: ``"tiled"``, ``"centre"`` (or ``"center"``) or ``"corner"``,
            in any letter case.

    Returns:
        One of :data:`LAYOUTS`.

    Raises:
        ValueError: If the layout is unknown.
    """
    key = name.strip().lower()
    key = LAYOUT_ALIASES.get(key, key)
    if key not in LAYOUTS:
        raise ValueError(f"Layout must be one of {', '.join(LAYOUTS)} (got '{name}')")
    return key


class FontSize(NamedTuple):
    """A text size, absolute or relative to the image.

    Attributes:
        value: Pixels, or a percentage when ``relative`` is true.
        relative: True when ``value`` is a percentage of the image's
            shorter side (so the watermark scales with the image).
    """

    value: float
    relative: bool

    def __str__(self) -> str:
        """Format the size the way it is typed, e.g. ``"60"`` or ``"5%"``."""
        return f"{self.value:g}%" if self.relative else f"{self.value:g}"


def parse_font_size(spec: "str | int | float | FontSize") -> FontSize:
    """Parse a text size such as ``60``, ``"60px"`` or ``"5%"``.

    Args:
        spec: Pixels (number or text, an optional ``px`` suffix is allowed)
            or a percentage of the image's shorter side (text ending in
            ``%``).

    Returns:
        The validated :class:`FontSize`.

    Raises:
        ValueError: If the text is not a number or is out of range.
    """
    text = str(spec).strip().lower().replace(" ", "")
    relative = text.endswith("%")
    number = text[:-1] if relative else text.removesuffix("px")
    try:
        value = float(number)
    except ValueError:
        raise ValueError(
            "Text size must be pixels (e.g. 60) or a percentage of the "
            f"image (e.g. 5%) (got '{spec}')"
        ) from None
    if relative:
        if not 0 < value <= 100:
            raise ValueError(
                f"Text size must be between 0% and 100% of the image (got {spec})"
            )
        return FontSize(value, True)
    if not 1 <= value <= MAX_FONT_PX:
        raise ValueError(
            f"Text size must be between 1 and {MAX_FONT_PX} px (got {spec})"
        )
    return FontSize(round(value), False)


def font_size_px(size: FontSize, image_size: Size) -> int:
    """Resolve a text size to pixels for a given image.

    Args:
        size: The (possibly relative) text size.
        image_size: ``(width, height)`` of the image in pixels.

    Returns:
        The text size in pixels (at least 1).
    """
    if size.relative:
        return max(1, round(min(image_size) * size.value / 100))
    return max(1, int(size.value))


def parse_color(value: str) -> RGBA:
    """Convert a user-supplied colour into an RGBA tuple.

    Accepts anything Pillow understands (``"white"``, ``"#c8c8c8"``,
    ``"#ff000080"`` with alpha, ``"rgb(200, 200, 200)"``...).

    Args:
        value: The colour text.

    Returns:
        An ``(r, g, b, a)`` tuple with 0-255 channels.

    Raises:
        ValueError: If the colour cannot be parsed.
    """
    try:
        rgb = ImageColor.getrgb(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid colour: '{value}'") from exc
    return (*rgb, 255) if len(rgb) == 3 else tuple(rgb)


def color_to_hex(color: RGBA) -> str:
    """Format the RGB part of a colour as ``#rrggbb`` (alpha is dropped).

    Args:
        color: An ``(r, g, b, a)`` tuple.

    Returns:
        The lowercase hexadecimal colour string.
    """
    return "#{:02x}{:02x}{:02x}".format(*color[:3])


def _relative_luminance(color: RGBA) -> float:
    """Compute the WCAG relative luminance of a colour (0 = black, 1 = white).

    Args:
        color: An ``(r, g, b, a)`` tuple; alpha is ignored.

    Returns:
        The relative luminance as a float.
    """

    def linear(channel: int) -> float:
        # Undo the sRGB gamma curve to get linear light intensity.
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in color[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def outline_color(color: RGBA) -> RGBA:
    """Pick the outline colour that contrasts best with the text colour.

    Args:
        color: The text colour.

    Returns:
        Opaque black for light text, opaque white for dark text.
    """
    # 0.18 is the luminance of a perceptual mid grey (L* = 50).
    return (0, 0, 0, 255) if _relative_luminance(color) > 0.18 else (255, 255, 255, 255)


@lru_cache(maxsize=None)
def resolve_font_file(font: str) -> str | None:
    """Find the font file for a generic family, or validate a font path.

    Args:
        font: ``"sans"``, ``"serif"``, ``"mono"`` or a path to a TrueType /
            OpenType font file (``.ttf``, ``.otf``, ``.ttc``).

    Returns:
        The font file path, or ``None`` when no system font of the family
        is installed (Pillow's built-in font is used instead).

    Raises:
        ValueError: If a font path does not exist or is not a font.
    """
    key = font.strip().lower()
    if key in FONT_FAMILIES:
        for name in FONT_FAMILIES[key]:
            try:
                # Loading by bare name makes Pillow search the font folders;
                # .path then tells where the file was found.
                return ImageFont.truetype(name, 12).path
            except OSError:
                continue
        return None
    path = Path(font.strip()).expanduser()
    if not path.is_file():
        raise ValueError(f"Font file not found: '{font}'")
    try:
        ImageFont.truetype(str(path), 12)
    except OSError as exc:
        raise ValueError(f"Not a usable font file: '{path.name}'") from exc
    return str(path)


def normalize_font(font: str) -> str:
    """Return the canonical font choice (family keyword or font file path).

    Args:
        font: Family keyword (any letter case) or font file path.

    Returns:
        ``"sans"``, ``"serif"``, ``"mono"`` or the font path.

    Raises:
        ValueError: If a font path is not usable.
    """
    key = font.strip().lower()
    if key in FONT_FAMILIES:
        return key
    if not font.strip():
        raise ValueError("Please choose a font.")
    return resolve_font_file(font)


@lru_cache(maxsize=64)
def load_font(font: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a font at a pixel size (cached, the preview asks often).

    Args:
        font: Family keyword or font path (see :func:`resolve_font_file`).
        size: Text size in pixels.

    Returns:
        The loaded font.
    """
    path = resolve_font_file(font)
    if path is None:
        return ImageFont.load_default(size=size)
    return ImageFont.truetype(path, size)


def font_display_name(font: str) -> str:
    """Return a human-readable name for a font choice, e.g. ``"Helvetica"``.

    Args:
        font: Family keyword or font path.

    Returns:
        The font's family name.

    Raises:
        ValueError: If a font path is not usable.
    """
    loaded = load_font(font, 12)
    try:
        return loaded.getname()[0] or Path(loaded.path).stem
    except AttributeError:  # bitmap fallback font without names
        return "built-in"


def _check_range(value: float, name: str, minimum: float, maximum: float) -> None:
    """Raise ``ValueError`` unless ``minimum <= value <= maximum``.

    Args:
        value: The number to check.
        name: Option name used in the error message.
        minimum: Smallest allowed value.
        maximum: Largest allowed value.
    """
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum:g} and {maximum:g} (got {value:g})"
        )


@dataclass(frozen=True)
class Watermark:
    """Validated watermark settings, independent of any particular image.

    Build it with :func:`make_watermark`, which checks every value.

    Attributes:
        text: The watermark text.
        font_size: Text size, in pixels or relative to the image.
        font: Family keyword or font file path.
        color: Text colour (its alpha multiplies ``opacity``).
        opacity: Opacity in percent, 0-100.
        angle: Rotation in degrees; positive turns the text counter-clockwise.
        spacing: Gap between tiled copies, in multiples of the text size.
        layout: One of :data:`LAYOUTS`.
        outline: Whether to draw a thin contrasting outline.
    """

    text: str
    font_size: FontSize
    font: str
    color: RGBA
    opacity: int
    angle: float
    spacing: float
    layout: str
    outline: bool


def make_watermark(
    text: str = DEFAULT_TEXT,
    font_size: "str | int | FontSize" = DEFAULT_FONT_SIZE,
    font: str = DEFAULT_FONT,
    color: str = DEFAULT_COLOR,
    opacity: int = DEFAULT_OPACITY,
    angle: float = DEFAULT_ANGLE,
    spacing: float = DEFAULT_SPACING,
    layout: str = DEFAULT_LAYOUT,
    outline: bool = False,
) -> Watermark:
    """Validate watermark options shared by the GUI and headless mode.

    Args:
        text: Watermark text; surrounding whitespace is stripped.
        font_size: Pixels (``60``) or a percentage (``"5%"``).
        font: ``"sans"``, ``"serif"``, ``"mono"`` or a font file path.
        color: Text colour (any Pillow colour string).
        opacity: Opacity in percent, 0-100.
        angle: Rotation in degrees, -90 to 90.
        spacing: Gap between tiled copies, in multiples of the text size.
        layout: ``"tiled"``, ``"centre"`` or ``"corner"``.
        outline: Draw a thin contrasting outline around the text.

    Returns:
        The validated :class:`Watermark`.

    Raises:
        ValueError: If any option is invalid.
    """
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("Please enter the watermark text.")
    rgba = parse_color(color)
    if rgba[3] == 0:
        raise ValueError("The text colour cannot be fully transparent.")
    _check_range(opacity, "Opacity", 0, 100)
    _check_range(angle, "Angle", -MAX_ANGLE, MAX_ANGLE)
    _check_range(spacing, "Spacing", 0, MAX_SPACING)
    return Watermark(
        text=clean_text,
        font_size=parse_font_size(font_size),
        font=normalize_font(font),
        color=rgba,
        opacity=int(opacity),
        angle=float(angle),
        spacing=float(spacing),
        layout=normalize_layout(layout),
        outline=bool(outline),
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


class Stamp(NamedTuple):
    """One rendered (and rotated) copy of the watermark text.

    Attributes:
        fill: Coverage mask (mode ``L``) of the letters.
        outline: Coverage mask of letters plus outline, or ``None``.
        text_size: ``(width, height)`` of the text before rotation, used
            to space the tiled copies.
    """

    fill: Image.Image
    outline: Image.Image | None
    text_size: Size

    @property
    def size(self) -> Size:
        """Size of the rotated stamp in pixels."""
        return self.fill.size


class WatermarkLayer(NamedTuple):
    """A watermark rendered for one image size.

    Attributes:
        image: Transparent ``RGBA`` layer to lay over the image.
        font_px: Text size used, in pixels of this layer.
        count: Number of text copies that touch the image.
        clipped: True when a single copy (centre/corner) does not fit.
    """

    image: Image.Image
    font_px: int
    count: int
    clipped: bool


def render_stamp(wm: Watermark, font_px: int) -> Stamp:
    """Draw the watermark text once, rotated, as coverage masks.

    Masks are used instead of coloured images so that copies can be merged
    with a plain "union" (see :func:`build_layer`) and the colour and
    opacity applied once at the end: overlapping copies never get darker.

    Args:
        wm: Watermark settings.
        font_px: Text size in pixels.

    Returns:
        The rendered :class:`Stamp`, cropped to its visible pixels.

    Raises:
        ValueError: If the font cannot draw the text.
    """
    render_px = max(font_px, MIN_RENDER_PX)
    stroke = max(1, round(render_px * OUTLINE_RATIO)) if wm.outline else 0
    try:
        font = load_font(wm.font, render_px)
        measure = ImageDraw.Draw(Image.new("L", (1, 1)))
        left, top, right, bottom = measure.textbbox(
            (0, 0), wm.text, font=font, stroke_width=stroke, align="center"
        )
        pad = 2  # keeps anti-aliased edges from touching the border
        canvas_size = (right - left + 2 * pad, bottom - top + 2 * pad)
        origin = (pad - left, pad - top)

        fill = Image.new("L", canvas_size, 0)
        ImageDraw.Draw(fill).text(origin, wm.text, font=font, fill=255, align="center")
        outline = None
        if stroke:
            outline = Image.new("L", canvas_size, 0)
            ImageDraw.Draw(outline).text(
                origin,
                wm.text,
                font=font,
                fill=255,
                stroke_width=stroke,
                stroke_fill=255,
                align="center",
            )
    except OSError as exc:  # FreeType errors, e.g. on broken fonts
        raise ValueError(f"Cannot draw the text with this font ({exc}).") from exc
    text_size = (right - left, bottom - top)

    if render_px != font_px:
        # Scale text drawn at the minimum size down to the requested size.
        factor = font_px / render_px
        small = tuple(max(1, round(side * factor)) for side in canvas_size)
        fill = fill.resize(small, Image.Resampling.LANCZOS)
        if outline is not None:
            outline = outline.resize(small, Image.Resampling.LANCZOS)
        text_size = tuple(max(1, round(side * factor)) for side in text_size)

    if wm.angle:
        # Both masks get the same rotation, so they stay aligned.
        fill = fill.rotate(wm.angle, Image.Resampling.BICUBIC, expand=True)
        if outline is not None:
            outline = outline.rotate(wm.angle, Image.Resampling.BICUBIC, expand=True)

    # Trim the empty corners left by the rotation (outline covers the fill).
    box = (outline or fill).getbbox()
    if box is not None:
        fill = fill.crop(box)
        if outline is not None:
            outline = outline.crop(box)
    return Stamp(fill, outline, text_size)


def stamp_positions(
    size: Size, stamp: Stamp, font_px: int, wm: Watermark
) -> list[tuple[int, int]]:
    """Compute where copies of the stamp go (top-left corners, in pixels).

    The tiled layout is a brick pattern laid out in the rotated text
    direction and centred on the image, so one copy always sits exactly in
    the middle and the corners are covered whatever the angle.

    Args:
        size: ``(width, height)`` of the image.
        stamp: The rendered text.
        font_px: Text size in pixels (sets the spacing gap).
        wm: Watermark settings (layout, angle, spacing).

    Returns:
        Top-left positions of every copy that touches the image. For the
        centre/corner layouts this is a single position, possibly partly
        outside the image when the text is too big.

    Raises:
        ValueError: If the tiled pattern would need too many copies.
    """
    width, height = size
    stamp_w, stamp_h = stamp.size
    if wm.layout == "centre":
        return [((width - stamp_w) // 2, (height - stamp_h) // 2)]
    if wm.layout == "corner":
        margin = round(min(width, height) * CORNER_MARGIN)
        return [(width - margin - stamp_w, height - margin - stamp_h)]

    gap = wm.spacing * font_px
    step_x = max(1.0, stamp.text_size[0] + gap)  # along the text direction
    step_y = max(1.0, stamp.text_size[1] + gap)  # between rows of text
    # Every lattice point whose stamp could touch the image lies within
    # this distance of the image centre.
    reach = math.hypot(width, height) / 2 + math.hypot(stamp_w, stamp_h) / 2
    cols = math.ceil(reach / step_x) + 1
    rows = math.ceil(reach / step_y) + 1
    if (2 * cols + 1) * (2 * rows + 1) > MAX_COPIES:
        raise ValueError(
            "The text is too small for a tiled pattern on this image; "
            "increase the text size or the spacing."
        )

    theta = math.radians(wm.angle)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    centre_x, centre_y = width / 2, height / 2
    positions = []
    for row in range(-rows, rows + 1):
        v = row * step_y
        shift = step_x / 2 if row % 2 else 0.0  # brick offset on odd rows
        for col in range(-cols, cols + 1):
            u = col * step_x + shift
            # (u, v) are coordinates along / across the text; rotate them
            # counter-clockwise (y points down) into image coordinates.
            x = centre_x + u * cos_t + v * sin_t
            y = centre_y - u * sin_t + v * cos_t
            left, top = round(x - stamp_w / 2), round(y - stamp_h / 2)
            if (
                left < width
                and top < height
                and left + stamp_w > 0
                and top + stamp_h > 0
            ):
                positions.append((left, top))
    return positions


def build_layer(size: Size, wm: Watermark, scale: float = 1.0) -> WatermarkLayer:
    """Render the watermark as a transparent layer for an image size.

    Args:
        size: ``(width, height)`` of the image the layer will cover.
        wm: Watermark settings.
        scale: Size of this image relative to the real one, below 1 for a
            preview thumbnail. The text size is resolved against the real
            image and then scaled, so the preview matches the saved file.

    Returns:
        The :class:`WatermarkLayer`.

    Raises:
        ValueError: If the tiled pattern would need too many copies.
    """
    full_size = (size[0] / scale, size[1] / scale)
    font_px = max(1, round(font_size_px(wm.font_size, full_size) * scale))
    stamp = render_stamp(wm, font_px)
    positions = stamp_positions(size, stamp, font_px, wm)
    stamp_w, stamp_h = stamp.size

    # Union of all copies: pasting a constant 255 through each stamp mask
    # gives new = 255 * m + old * (1 - m), the coverage of either one.
    fill_mask = Image.new("L", size, 0)
    outline_mask = Image.new("L", size, 0) if stamp.outline is not None else None
    for left, top in positions:
        box = (left, top, left + stamp_w, top + stamp_h)
        fill_mask.paste(255, box, stamp.fill)
        if outline_mask is not None:
            outline_mask.paste(255, box, stamp.outline)

    if outline_mask is None:
        layer = Image.new("RGBA", size, wm.color[:3] + (255,))
        coverage = fill_mask
    else:
        # Outline colour everywhere, text colour through the letter mask
        # (anti-aliased letter edges blend between the two).
        layer = Image.new("RGBA", size, outline_color(wm.color))
        layer.paste(wm.color[:3] + (255,), (0, 0, *size), fill_mask)
        coverage = outline_mask

    # Opacity (and the colour's own alpha) scale the coverage once.
    strength = wm.opacity / 100 * wm.color[3] / 255
    layer.putalpha(coverage.point([round(v * strength) for v in range(256)]))

    clipped = wm.layout != "tiled" and any(
        left < 0 or top < 0 or left + stamp_w > size[0] or top + stamp_h > size[1]
        for left, top in positions
    )
    return WatermarkLayer(layer, font_px, len(positions), clipped)


def _rgb_or_rgba(image: Image.Image) -> Image.Image:
    """Convert an image to ``RGBA`` if it has transparency, else ``RGB``.

    Args:
        image: Any Pillow image.

    Returns:
        The image itself when already RGB/RGBA, else a converted copy.
    """
    if image.mode in ("RGB", "RGBA"):
        return image
    if image.mode == "I" or image.mode.startswith("I;16"):
        # 16-bit greyscale (common for scientific images): scale 0-65535
        # down to 0-255, a plain conversion would clip almost all to white.
        image = image.convert("I")
        if image.getextrema()[1] > 255:
            image = image.point(lambda value: value / 256)
        image = image.convert("L")
    return image.convert("RGBA" if image.has_transparency_data else "RGB")


def composite(image: Image.Image, layer: Image.Image) -> Image.Image:
    """Lay a watermark layer over an image.

    Args:
        image: ``RGB`` or ``RGBA`` image.
        layer: ``RGBA`` layer of the same size.

    Returns:
        A new image in the mode of ``image``.
    """
    if image.mode == "RGBA":
        return Image.alpha_composite(image, layer)
    result = image.copy()
    result.paste(layer, (0, 0), layer)  # the layer's alpha is the mask
    return result


def apply_watermark(
    image: Image.Image, wm: Watermark, scale: float = 1.0
) -> Image.Image:
    """Return a watermarked copy of an image.

    Args:
        image: The image (any mode; it is converted to RGB or RGBA).
        wm: Watermark settings.
        scale: See :func:`build_layer` (1 for the real image).

    Returns:
        The watermarked image, ``RGB`` or ``RGBA`` like the input.

    Raises:
        ValueError: If the tiled pattern would need too many copies.
    """
    image = _rgb_or_rgba(image)
    return composite(image, build_layer(image.size, wm, scale).image)


def watermark_warnings(
    image_size: Size,
    wm: Watermark,
    fmt: str | None = None,
    has_transparency: bool = False,
) -> list[str]:
    """List settings that probably will not give the intended result.

    Args:
        image_size: ``(width, height)`` of the real image.
        wm: Watermark settings.
        fmt: Output format, to check that transparency can be stored.
        has_transparency: Whether the image has transparent pixels.

    Returns:
        Human-readable warnings (empty when everything looks fine).
    """
    warnings = []
    if wm.opacity == 0:
        warnings.append("Opacity is 0%: the watermark will be invisible.")
    elif wm.opacity < 10:
        warnings.append("Opacity is very low: the watermark will be hard to see.")
    if wm.layout != "tiled":
        font_px = font_size_px(wm.font_size, image_size)
        stamp = render_stamp(wm, font_px)
        ((left, top),) = stamp_positions(image_size, stamp, font_px, wm)
        if (
            left < 0
            or top < 0
            or left + stamp.size[0] > image_size[0]
            or top + stamp.size[1] > image_size[1]
        ):
            warnings.append(
                "The text does not fit in the image and will be cut off; "
                "make it smaller or shorter."
            )
    if fmt is not None and has_transparency and not FORMATS[fmt].supports_alpha:
        warnings.append(
            f"{fmt.upper()} cannot store transparency: transparent areas "
            f"will become white."
        )
    return warnings


# --------------------------------------------------------------------------
# Reading and writing files
# --------------------------------------------------------------------------


class SourceImage(NamedTuple):
    """An image opened for watermarking.

    Attributes:
        image: Pixel data, upright (EXIF orientation applied), ``RGB`` or
            ``RGBA`` (only when some pixels are actually transparent).
        path: The file it was read from.
        format: Output format matching the file's format, or ``None`` when
            that format cannot be written (e.g. GIF).
        icc_profile: Colour profile to embed in the output, if any.
        dpi: Print resolution to keep in the output, if known.
    """

    image: Image.Image
    path: Path
    format: str | None
    icc_profile: bytes | None
    dpi: tuple[float, float] | None

    @property
    def has_transparency(self) -> bool:
        """Whether the image has transparent pixels."""
        return self.image.mode == "RGBA"


def open_image(path: "str | Path") -> SourceImage:
    """Open an image file and prepare it for watermarking.

    Args:
        path: Image file path.

    Returns:
        The :class:`SourceImage`.

    Raises:
        ValueError: If the file is missing or not a readable image.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"Image not found: {path}")
    try:
        with Image.open(path) as opened:
            pil_format = opened.format
            # Phone photos are often stored sideways plus an "orientation"
            # tag; turn the pixels upright so the text is placed as seen.
            upright = ImageOps.exif_transpose(opened)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Cannot open '{path.name}' as an image.") from exc

    # The profile only describes the pixels if they stay in the same
    # colour space (a CMYK or greyscale profile would be wrong after RGB).
    icc_profile = (
        upright.info.get("icc_profile") if upright.mode in ("RGB", "RGBA") else None
    )
    dpi = upright.info.get("dpi")
    image = _rgb_or_rgba(upright)
    if image.mode == "RGBA" and image.getchannel("A").getextrema()[0] == 255:
        image = image.convert("RGB")  # an alpha channel with nothing transparent
    return SourceImage(
        image=image,
        path=path,
        format=SOURCE_FORMATS.get(pil_format or ""),
        icc_profile=icc_profile,
        dpi=tuple(dpi) if dpi else None,
    )


def flatten(image: Image.Image, background: str = "white") -> Image.Image:
    """Remove transparency by laying an image onto a solid background.

    Args:
        image: ``RGBA`` image.
        background: Background colour.

    Returns:
        An ``RGB`` image.
    """
    flat = Image.new("RGB", image.size, background)
    flat.paste(image, (0, 0), image)
    return flat


def save_image(
    image: Image.Image,
    path: Path,
    fmt: str,
    quality: int = DEFAULT_QUALITY,
    icc_profile: bytes | None = None,
    dpi: tuple[float, float] | None = None,
) -> None:
    """Save an image with sensible per-format settings.

    Args:
        image: ``RGB`` or ``RGBA`` image.
        path: Destination file path.
        fmt: Canonical format key.
        quality: JPG/WEBP quality, 1-100 (ignored by lossless formats).
        icc_profile: Colour profile to embed (not supported by BMP).
        dpi: Print resolution to store (not supported by WEBP).
    """
    spec = FORMATS[fmt]
    if image.mode == "RGBA" and not spec.supports_alpha:
        image = flatten(image)
    options: dict = {}
    if spec.lossy:
        options["quality"] = quality
    if fmt == "jpg":
        options["optimize"] = True
    elif fmt == "tiff":
        options["compression"] = "tiff_lzw"
    if icc_profile and fmt != "bmp":
        options["icc_profile"] = icc_profile
    if dpi and fmt != "webp":
        options["dpi"] = dpi
    image.save(path, format=spec.pil_name, **options)


def is_same_file(first: Path, second: Path) -> bool:
    """Tell whether two paths point to the same file.

    Args:
        first: A path (may not exist yet).
        second: Another path.

    Returns:
        True when both name the same file (case-insensitive file systems
        and symlinks included).
    """
    first, second = Path(first).expanduser(), Path(second).expanduser()
    try:
        if first.exists() and second.exists():
            return first.samefile(second)
    except OSError:
        pass
    return first.resolve() == second.resolve()


def default_output_name(input_path: Path) -> str:
    """Return the default output file name (without extension).

    Args:
        input_path: The image being watermarked.

    Returns:
        ``<input stem>_watermarked``.
    """
    return f"{Path(input_path).stem}{OUTPUT_SUFFIX}"


def resolve_output_path(
    output: str | None,
    input_path: Path,
    fmt: str | None = None,
    source_format: str | None = None,
) -> tuple[Path, str]:
    """Work out the output file path and format for headless mode.

    Rules:
        * no ``output``: ``<input stem>_watermarked`` next to the input;
        * an existing folder as ``output``: that default name inside it;
        * the format comes from ``fmt``, else the output's extension, else
          the input's format, else PNG;
        * an output without a known extension gets ``.<fmt>`` appended;
        * an extension that contradicts an explicit ``fmt`` is an error.

    Args:
        output: File name, path or folder given by the user, or ``None``.
        input_path: The image being watermarked.
        fmt: Explicit output format, or ``None`` to infer it.
        source_format: Output format matching the input file, if any.

    Returns:
        Tuple of (output path, canonical format key).

    Raises:
        ValueError: If the format is unknown or conflicts with the extension.
    """
    input_path = Path(input_path).expanduser()
    explicit = normalize_format(fmt) if fmt else None
    if output is None:
        path = input_path.with_name(default_output_name(input_path))
    else:
        path = Path(output).expanduser()
        if path.is_dir():
            path = path / default_output_name(input_path)
    suffix_format = format_from_suffix(path)
    if explicit and suffix_format and explicit != suffix_format:
        raise ValueError(
            f"Output name '{path.name}' does not match the requested format '{explicit}'"
        )
    fmt = explicit or suffix_format or source_format or DEFAULT_FORMAT
    if suffix_format is None:
        # Append rather than replace, so dots inside a name are preserved.
        path = path.with_name(f"{path.name}.{fmt}")
    return path, fmt


def export_watermarked(
    source: SourceImage,
    output_path: Path,
    wm: Watermark,
    fmt: str,
    quality: int = DEFAULT_QUALITY,
) -> Path:
    """Watermark an opened image at full resolution and write it to disk.

    This is the single entry point shared by the GUI and headless mode.
    Missing parent folders of ``output_path`` are created; an existing file
    is replaced, except the original image, which is never overwritten.

    Args:
        source: The opened image.
        output_path: Destination file (used as-is; no extension is added).
        wm: Watermark settings.
        fmt: Output format (any key or alias of :data:`FORMATS`).
        quality: JPG/WEBP quality, 1-100.

    Returns:
        The path of the written file.

    Raises:
        ValueError: If an option is invalid or the output is the original.
        OSError: If the file cannot be written.
    """
    fmt = normalize_format(fmt)
    _check_range(quality, "Quality", 1, 100)
    output_path = Path(output_path).expanduser()
    if is_same_file(output_path, source.path):
        raise ValueError(
            "The output would replace the original image; choose another name."
        )
    result = apply_watermark(source.image, wm)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_image(result, output_path, fmt, quality, source.icc_profile, source.dpi)
    return output_path


def clean_path_text(text: str) -> str:
    """Tidy a path pasted or dropped into a text field.

    Handles surrounding quotes (``"Copy as path"`` on Windows), ``file://``
    URLs, and spaces escaped with backslashes (paths dragged into a
    terminal on macOS/Linux).

    Args:
        text: The raw field content.

    Returns:
        A plain path string ("" if the field is blank).
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if text.startswith("file://"):
        text = unquote(urlparse(text).path)
    if "\\ " in text and not Path(text).expanduser().exists():
        text = text.replace("\\ ", " ")
    return text


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

# GUI colours as (light mode, dark mode) pairs, shared with the QR code
# generator: Apple's system palette, so the window feels native on macOS
# and still looks clean elsewhere.
PALETTE = {
    "window": ("#f2f2f7", "#1c1c1e"),
    "card": ("#ffffff", "#2c2c2e"),
    "tile": ("#f2f2f7", "#1c1c1e"),
    "field": ("#f7f7fa", "#3a3a3c"),
    "field_border": ("#d8d8de", "#4a4a4e"),
    "track": ("#e7e7ec", "#3a3a3c"),
    "track_hover": ("#d8d8de", "#48484a"),
    "switch_off": ("#d1d1d6", "#545458"),
    "knob": ("#ffffff", "#f5f5f7"),
    "selected": ("#ffffff", "#69696e"),
    "text": ("#1c1c1e", "#f5f5f7"),
    "muted": ("#6e6e73", "#98989d"),
    "accent": ("#007aff", "#0a84ff"),
    "accent_hover": ("#0062cc", "#409cff"),
    "success": ("#248a3d", "#30d158"),
    "warning": ("#b25000", "#ffb340"),
    "error": ("#d70015", "#ff6961"),
}

# Light/dark squares used to show transparent areas in the preview.
CHECKER_COLORS = ("#ffffff", "#e3e3e8")

# Label of the appearance switch -> customtkinter appearance mode.
APPEARANCE_MODES = {"Auto": "system", "Light": "light", "Dark": "dark"}

# GUI labels of the font and layout selectors -> option values.
FONT_LABELS = {"Sans": "sans", "Serif": "serif", "Mono": "mono"}
LAYOUT_LABELS = {"Tiled": "tiled", "Centred": "centre", "Corner": "corner"}

# Short explanation shown next to the layout selector.
LAYOUT_HINTS = {
    "tiled": "repeated over the image",
    "centre": "one copy in the middle",
    "corner": "bottom-right signature",
}

# File types offered by the "Open image" dialog.
IMAGE_FILETYPES = [
    ("Images", "*.png *.jpg *.jpeg *.webp *.tif *.tiff *.bmp *.gif"),
    ("All files", "*"),
]
FONT_FILETYPES = [("Fonts", "*.ttf *.otf *.ttc"), ("All files", "*")]

IS_MAC = sys.platform == "darwin"


def _to_int(text: str, name: str) -> int:
    """Parse a whole number typed into a GUI field.

    Args:
        text: The field content.
        name: Field name used in the error message.

    Returns:
        The parsed integer.

    Raises:
        ValueError: If the text is not a whole number.
    """
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number (got '{text}')") from None


def fit_size(size: Size, box: int) -> Size:
    """Scale a size to fit inside a square, keeping its aspect ratio.

    Small images are scaled up too, so the preview always fills the tile.

    Args:
        size: ``(width, height)`` to fit.
        box: Side of the square in pixels.

    Returns:
        The fitted ``(width, height)``, each at least 1.
    """
    ratio = min(box / size[0], box / size[1])
    return max(1, round(size[0] * ratio)), max(1, round(size[1] * ratio))


def shorten_home(path: Path) -> str:
    """Display a path with the home folder abbreviated to ``~``.

    Args:
        path: Any path.

    Returns:
        The path as text, e.g. ``~/Pictures/photo.jpg``.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def reveal_in_file_manager(path: Path) -> None:
    """Show a file in Finder / Explorer (or open its folder on Linux).

    Args:
        path: The file to reveal.
    """
    if IS_MAC:
        command = ["open", "-R", str(path)]
    elif sys.platform == "win32":
        command = ["explorer", f"/select,{path}"]
    else:
        command = ["xdg-open", str(path.parent)]
    subprocess.Popen(command)  # fire and forget; the GUI must not block


class WatermarkApp:
    """Modern CustomTkinter window for watermarking images with a live preview.

    The widgets only collect options; all validation and rendering is
    delegated to the same functions used by headless mode. The preview is
    rendered on a thumbnail, with the text size scaled to match the file
    that will be saved.
    """

    PREVIEW_SIZE = 336  # side of the preview drawing area in pixels
    TILE_SIZE = 360  # side of the rounded tile around the preview
    MESSAGE_AREA_HEIGHT = 64  # room for ~3 lines of status text
    FIELD_HEIGHT = 32
    SLIDER_WIDTH = 232
    REFRESH_DELAY_MS = 150  # debounce delay between typing and re-rendering
    SIZE_PRESETS = ["3%", "5%", "8%", "12%"]

    def __init__(
        self,
        root: "ctk.CTk",
        image_path: str = "",
        output_dir: Path | None = None,
        name: str | None = None,
        fmt: str | None = None,
        text: str = DEFAULT_TEXT,
        font: str = DEFAULT_FONT,
        font_size: str = DEFAULT_FONT_SIZE,
        color: str = DEFAULT_COLOR,
        opacity: int = DEFAULT_OPACITY,
        angle: float = DEFAULT_ANGLE,
        spacing: float = DEFAULT_SPACING,
        layout: str = DEFAULT_LAYOUT,
        outline: bool = False,
        quality: int = DEFAULT_QUALITY,
    ):
        """Build the window and pre-fill it with the given options.

        ``output_dir``, ``name`` and ``fmt`` default to the opened image's
        folder, ``<stem>_watermarked`` and format; values given here are
        kept for the first image (later images use the defaults again).

        Args:
            root: The CustomTkinter root window to populate.
            image_path: Initial image file ("" for none).
            output_dir: Initial output folder.
            name: Initial output file name (without extension).
            fmt: Initial output format.
            text: Initial watermark text.
            font: Initial font (family keyword or font path).
            font_size: Initial text size (``"60"`` px or ``"5%"``).
            color: Initial text colour.
            opacity: Initial opacity in percent.
            angle: Initial angle in degrees.
            spacing: Initial spacing in multiples of the text size.
            layout: Initial layout.
            outline: Initial outline switch state.
            quality: Initial JPG/WEBP quality.

        Raises:
            ValueError: If ``fmt`` or ``layout`` is unknown.
        """
        self.root = root
        root.title("Watermark")
        root.configure(fg_color=PALETTE["window"])

        # Tk variables backing every input field.
        self.image_var = tk.StringVar(value=image_path)
        self.text_var = tk.StringVar(value=text)
        self.font_var = tk.StringVar(value=font)
        self.size_var = tk.StringVar(value=str(font_size))
        self.color_var = tk.StringVar(value=color)
        self.outline_var = tk.BooleanVar(value=outline)
        self.opacity_var = tk.IntVar(value=opacity)
        self.layout_var = tk.StringVar(value=normalize_layout(layout))
        self.angle_var = tk.IntVar(value=round(angle))
        self.spacing_var = tk.DoubleVar(value=spacing)
        self.format_var = tk.StringVar(
            value=normalize_format(fmt or DEFAULT_FORMAT).upper()
        )
        self.quality_var = tk.StringVar(value=str(quality))
        self.name_var = tk.StringVar(value=name or "")
        self.folder_var = tk.StringVar(value=str(output_dir or Path.cwd()))
        self.appearance_var = tk.StringVar(value="Auto")
        self.target_var = tk.StringVar()
        self.status_var = tk.StringVar()

        # Which output fields follow the next opened image (see _adopt).
        self._adopt = {
            "name": name is None,
            "folder": output_dir is None,
            "fmt": fmt is None,
        }

        self.source: SourceImage | None = None
        self._source_text = ""  # field content the current source came from
        self._load_error: tuple[str, str] | None = None  # (field text, message)
        self._preview_base: Image.Image | None = None
        self._preview_scale = 1.0
        self._photos: dict[str, "ImageTk.PhotoImage"] = {}  # keep refs alive
        self._comparing = False
        self._pending_refresh = None
        self._destroyed = False
        self._last_saved: Path | None = None
        self.fonts = {
            "title": ctk.CTkFont(size=22, weight="bold"),
            "subtitle": ctk.CTkFont(size=13),
            "section": ctk.CTkFont(size=11, weight="bold"),
            "body": ctk.CTkFont(size=13),
            "small": ctk.CTkFont(size=12),
            "mono": ctk.CTkFont(family="Menlo" if IS_MAC else "Consolas", size=12),
            "link": ctk.CTkFont(size=15),
            "button": ctk.CTkFont(size=15, weight="bold"),
        }

        self._build_widgets()

        # Re-render the preview whenever any option changes.
        for var in (
            self.image_var,
            self.text_var,
            self.font_var,
            self.size_var,
            self.color_var,
            self.outline_var,
            self.opacity_var,
            self.layout_var,
            self.angle_var,
            self.spacing_var,
            self.format_var,
            self.quality_var,
            self.name_var,
            self.folder_var,
        ):
            var.trace_add("write", self._schedule_refresh)
        self.image_var.trace_add("write", self._update_image_placeholder)
        self.font_var.trace_add("write", self._sync_font_controls)
        self.size_var.trace_add("write", self._sync_size_presets)
        self.layout_var.trace_add("write", self._on_layout_change)
        self.format_var.trace_add("write", self._on_format_change)
        for var in (self.opacity_var, self.angle_var, self.spacing_var):
            var.trace_add("write", self._update_slider_labels)

        # Redraw the (manually coloured) preview canvas on light/dark switches.
        ctk.AppearanceModeTracker.add(self._on_appearance_change, root)
        self.preview_canvas.bind("<Destroy>", self._on_destroy)

        self._update_image_placeholder()
        self._sync_font_controls()
        self._sync_size_presets()
        self._on_layout_change()
        self._on_format_change()
        self._update_slider_labels()
        self.refresh_preview()

        # Never let the window shrink below the size its content needs.
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

    # ----- small widget factories -------------------------------------------

    @staticmethod
    def _themed(pair: tuple[str, str]) -> str:
        """Pick the light or dark variant of a palette colour.

        Only needed for plain Tk widgets; CTk widgets accept the pair as-is.

        Args:
            pair: ``(light, dark)`` colour pair from :data:`PALETTE`.

        Returns:
            The colour matching the current appearance mode.
        """
        return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]

    def _card(self, parent) -> "ctk.CTkFrame":
        """Create a rounded white (dark: grey) card.

        Args:
            parent: Parent widget.

        Returns:
            The card frame.
        """
        return ctk.CTkFrame(parent, corner_radius=16, fg_color=PALETTE["card"])

    def _label(
        self, parent, text: str, muted: bool = False, font: str = "body", **kwargs
    ):
        """Create a text label.

        Args:
            parent: Parent widget.
            text: Label text.
            muted: Use the secondary (grey) text colour.
            font: Key of :attr:`fonts`.
            **kwargs: Extra CTkLabel options.

        Returns:
            The label widget.
        """
        options = {"anchor": "w"}
        options.update(kwargs)
        return ctk.CTkLabel(
            parent,
            text=text,
            font=self.fonts[font],
            text_color=PALETTE["muted" if muted else "text"],
            **options,
        )

    def _entry(self, parent, var=None, width: int = 140, mono: bool = False, **kwargs):
        """Create a flat, rounded input field.

        Args:
            parent: Parent widget.
            var: Optional Tk variable bound to the field.
            width: Field width in pixels.
            mono: Use a monospaced font (hex colours).
            **kwargs: Extra CTkEntry options.

        Returns:
            The entry widget.
        """
        return ctk.CTkEntry(
            parent,
            textvariable=var,
            width=width,
            height=self.FIELD_HEIGHT,
            corner_radius=8,
            border_width=1,
            fg_color=PALETTE["field"],
            border_color=PALETTE["field_border"],
            text_color=PALETTE["text"],
            placeholder_text_color=PALETTE["muted"],
            font=self.fonts["mono" if mono else "body"],
            **kwargs,
        )

    def _segmented(self, parent, values: list[str], width: int, **kwargs):
        """Create an iOS-style segmented control (grey track, white pill).

        Args:
            parent: Parent widget.
            values: Segment labels.
            width: Total width in pixels (segments share it equally).
            **kwargs: Extra CTkSegmentedButton options (variable, command).

        Returns:
            The segmented button widget.
        """
        return ctk.CTkSegmentedButton(
            parent,
            values=values,
            width=width,
            height=self.FIELD_HEIGHT,
            corner_radius=8,
            border_width=3,
            dynamic_resizing=False,
            fg_color=PALETTE["track"],
            selected_color=PALETTE["selected"],
            selected_hover_color=PALETTE["selected"],
            unselected_color=PALETTE["track"],
            unselected_hover_color=PALETTE["track_hover"],
            text_color=PALETTE["text"],
            font=self.fonts["small"],
            **kwargs,
        )

    def _secondary_button(self, parent, text: str, command, width: int = 84, **kwargs):
        """Create a grey, low-emphasis button.

        Args:
            parent: Parent widget.
            text: Button text.
            command: Click callback.
            width: Button width in pixels.
            **kwargs: Extra CTkButton options.

        Returns:
            The button widget.
        """
        options = {
            "height": self.FIELD_HEIGHT,
            "corner_radius": 8,
            "fg_color": PALETTE["track"],
            "hover_color": PALETTE["track_hover"],
            "text_color": PALETTE["text"],
            "font": self.fonts["body"],
        }
        options.update(kwargs)
        return ctk.CTkButton(parent, text=text, command=command, width=width, **options)

    def _switch(self, parent, text: str, var) -> "ctk.CTkSwitch":
        """Create an iOS-style on/off switch.

        Args:
            parent: Parent widget.
            text: Label shown right of the switch.
            var: Boolean variable bound to the switch.

        Returns:
            The switch widget.
        """
        return ctk.CTkSwitch(
            parent,
            text=text,
            variable=var,
            onvalue=True,
            offvalue=False,
            switch_width=38,
            switch_height=22,
            progress_color=PALETTE["accent"],
            fg_color=PALETTE["switch_off"],
            button_color=PALETTE["knob"],
            button_hover_color=PALETTE["knob"],
            text_color=PALETTE["text"],
            font=self.fonts["body"],
        )

    def _slider(self, parent, var, from_: float, to: float, steps: int):
        """Create a slider followed by a fixed-width value label.

        Args:
            parent: Row frame (pack layout).
            var: Variable bound to the slider.
            from_: Value at the left end.
            to: Value at the right end.
            steps: Number of discrete steps between the ends.

        Returns:
            The (slider, value label) widgets.
        """
        slider = ctk.CTkSlider(
            parent,
            from_=from_,
            to=to,
            number_of_steps=steps,
            variable=var,
            width=self.SLIDER_WIDTH,
            height=18,
            fg_color=PALETTE["track"],
            progress_color=PALETTE["accent"],
            button_color=PALETTE["accent"],
            button_hover_color=PALETTE["accent_hover"],
        )
        slider.pack(side="left")
        value = self._label(parent, "", muted=True, width=56)
        value.pack(side="left", padx=(12, 0))
        return slider, value

    # ----- layout -------------------------------------------------------------

    def _build_widgets(self) -> None:
        """Create and lay out the whole window."""
        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        self._build_header(main)
        self._build_image_bar(main)

        cards = ctk.CTkFrame(main, fg_color="transparent")
        cards.grid(row=2, column=0, sticky="nsew")
        cards.columnconfigure(0, weight=1)
        cards.rowconfigure(0, weight=1)
        self._build_settings_card(cards)
        self._build_preview_card(cards)

        # Keyboard shortcuts: Cmd/Ctrl+O opens an image, Cmd/Ctrl+S saves,
        # Return in the image field loads the typed path at once.
        self.image_entry.bind("<Return>", lambda _e: self.refresh_preview())
        self.text_entry.bind("<Return>", self._on_save)
        modifiers = ("Command", "Control") if IS_MAC else ("Control",)
        for key, handler in (("o", self._open_image), ("s", self._on_save)):
            for modifier in modifiers:
                self.root.bind(f"<{modifier}-{key}>", lambda _e, h=handler: h())
        (self.text_entry if self.image_var.get() else self.image_entry).focus_set()

    def _build_header(self, parent) -> None:
        """Create the title, subtitle and the light/dark appearance switch.

        Args:
            parent: Frame the header is placed in.
        """
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="we", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        self._label(header, "Watermark", font="title").grid(row=0, column=0, sticky="w")
        self._label(
            header,
            "Open an image, add your text, and export a protected copy.",
            muted=True,
            font="subtitle",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._segmented(
            header,
            list(APPEARANCE_MODES),
            width=180,
            variable=self.appearance_var,
            command=self._set_appearance,
        ).grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_image_bar(self, parent) -> None:
        """Create the large image-path field with its Open / Clear buttons.

        Args:
            parent: Frame the image bar is placed in.
        """
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="we", pady=(0, 18))
        bar.columnconfigure(0, weight=1)

        self.image_entry = ctk.CTkEntry(
            bar,
            textvariable=self.image_var,
            height=48,
            corner_radius=12,
            border_width=1,
            fg_color=PALETTE["card"],
            border_color=PALETTE["field_border"],
            text_color=PALETTE["text"],
            font=self.fonts["link"],
        )
        self.image_entry.grid(row=0, column=0, sticky="we")

        # CTkEntry hides its placeholder while focused, but the field is
        # focused on start-up. A label laid over the field keeps the hint
        # visible until something is typed, like native macOS text fields.
        self.image_placeholder = ctk.CTkLabel(
            bar,
            text="Open an image, or paste its path here...",
            font=self.fonts["link"],
            text_color=PALETTE["muted"],
            fg_color=PALETTE["card"],
            height=24,
            anchor="w",
        )
        self.image_placeholder.bind(
            "<Button-1>", lambda _e: self.image_entry.focus_set()
        )

        ctk.CTkButton(
            bar,
            text="Open image...",
            command=self._open_image,
            width=128,
            height=48,
            corner_radius=12,
            font=self.fonts["body"],
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            text_color="#ffffff",
        ).grid(row=0, column=1, padx=(10, 0))
        self._secondary_button(
            bar,
            "Clear",
            lambda: self.set_image_path(""),
            width=72,
            height=48,
            corner_radius=12,
        ).grid(row=0, column=2, padx=(8, 0))

    def _section(self, card, row: int, title: str, first: bool = False) -> None:
        """Add a small upper-case section heading to a card.

        Args:
            card: Card frame (grid layout).
            row: Grid row to use.
            title: Heading text.
            first: True for the first heading (less space above it).
        """
        self._label(card, title.upper(), muted=True, font="section").grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="w",
            padx=22,
            pady=(20 if first else 16, 6),
        )

    def _field_row(self, card, row: int, label: str) -> "ctk.CTkFrame":
        """Add a "label | controls" row to a card.

        Args:
            card: Card frame (grid layout).
            row: Grid row to use.
            label: Text of the left-hand label.

        Returns:
            A transparent frame for the row's controls (pack layout).
        """
        self._label(card, label).grid(
            row=row, column=0, sticky="w", padx=(22, 12), pady=4
        )
        controls = ctk.CTkFrame(card, fg_color="transparent")
        controls.grid(row=row, column=1, sticky="we", padx=(0, 22), pady=4)
        return controls

    def _build_settings_card(self, parent) -> None:
        """Create the settings card (text, layout, export).

        Args:
            parent: Frame the card is placed in.
        """
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        card.columnconfigure(1, weight=1)

        self._section(card, 0, "Text", first=True)
        row = self._field_row(card, 1, "Text")
        self.text_entry = self._entry(row, self.text_var)
        self.text_entry.pack(side="left", fill="x", expand=True)

        row = self._field_row(card, 2, "Font")
        self.font_presets = self._segmented(
            row, list(FONT_LABELS), width=192, command=self._choose_font_family
        )
        self.font_presets.pack(side="left")
        self._secondary_button(row, "Other...", self._browse_font, width=76).pack(
            side="left", padx=(8, 0)
        )
        self.font_hint = self._label(row, "", muted=True, font="small")
        self.font_hint.pack(side="left", padx=(12, 0))

        row = self._field_row(card, 3, "Size")
        self.size_presets = self._segmented(
            row, self.SIZE_PRESETS, width=192, command=self.size_var.set
        )
        self.size_presets.pack(side="left")
        self._entry(row, self.size_var, width=64).pack(side="left", padx=(10, 0))
        self.size_hint = self._label(row, "", muted=True, font="small")
        self.size_hint.pack(side="left", padx=(10, 0))

        row = self._field_row(card, 4, "Colour")
        self.color_swatch = ctk.CTkButton(
            row,
            text="",
            width=self.FIELD_HEIGHT,
            height=self.FIELD_HEIGHT,
            corner_radius=8,
            border_width=1,
            border_color=PALETTE["field_border"],
            text_color=PALETTE["error"],
            font=self.fonts["body"],
            command=self._pick_color,
        )
        self.color_swatch.pack(side="left")
        self.color_entry = self._entry(row, self.color_var, width=96, mono=True)
        self.color_entry.pack(side="left", padx=(8, 0))
        self.outline_switch = self._switch(row, "Outline", self.outline_var)
        self.outline_switch.pack(side="left", padx=(18, 0))

        row = self._field_row(card, 5, "Opacity")
        self.opacity_slider, self.opacity_label = self._slider(
            row, self.opacity_var, 0, 100, 100
        )

        self._section(card, 6, "Layout")
        row = self._field_row(card, 7, "Pattern")
        self.layout_selector = self._segmented(
            row, list(LAYOUT_LABELS), width=240, command=self._choose_layout
        )
        self.layout_selector.pack(side="left")
        self.layout_hint = self._label(row, "", muted=True, font="small")
        self.layout_hint.pack(side="left", padx=(12, 0))

        row = self._field_row(card, 8, "Angle")
        self.angle_slider, self.angle_label = self._slider(
            row, self.angle_var, -MAX_ANGLE, MAX_ANGLE, 2 * MAX_ANGLE
        )

        row = self._field_row(card, 9, "Spacing")
        self.spacing_slider, self.spacing_label = self._slider(
            row, self.spacing_var, 0, 6, 24
        )

        self._section(card, 10, "Export")
        row = self._field_row(card, 11, "Format")
        self._segmented(
            row, [f.upper() for f in FORMATS], width=280, variable=self.format_var
        ).pack(side="left")
        self._label(row, "Quality").pack(side="left", padx=(16, 8))
        self.quality_entry = self._entry(row, self.quality_var, width=52)
        self.quality_entry.pack(side="left")

        row = self._field_row(card, 12, "File name")
        self._entry(row, self.name_var).pack(side="left", fill="x", expand=True)
        self.extension_label = self._label(row, "", muted=True)
        self.extension_label.pack(side="left", padx=(8, 0))
        row = self._field_row(card, 13, "Folder")
        self._entry(row, self.folder_var).pack(side="left", fill="x", expand=True)
        self._secondary_button(row, "Browse", self._browse_folder).pack(
            side="left", padx=(8, 0)
        )

        # Bottom padding of the card (an empty row with a minimum height).
        card.rowconfigure(14, minsize=16)

    def _build_preview_card(self, parent) -> None:
        """Create the preview card with the image tile and the save button.

        Args:
            parent: Frame the card is placed in.
        """
        card = self._card(parent)
        card.grid(row=0, column=1, sticky="ns")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)  # pushes the save area to the bottom

        self._label(card, "PREVIEW", muted=True, font="section").grid(
            row=0, column=0, sticky="w", padx=22, pady=(20, 8)
        )

        tile = ctk.CTkFrame(
            card,
            width=self.TILE_SIZE,
            height=self.TILE_SIZE,
            corner_radius=18,
            fg_color=PALETTE["tile"],
        )
        tile.grid(row=1, column=0, padx=22)
        self.preview_canvas = tk.Canvas(
            tile,
            width=self.PREVIEW_SIZE,
            height=self.PREVIEW_SIZE,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self.preview_canvas.place(relx=0.5, rely=0.5, anchor="center")
        # Click the empty tile to open an image; press and hold a preview
        # to see the original image, release to see the watermark again.
        self.preview_canvas.bind("<ButtonPress-1>", self._on_preview_press)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_preview_release)

        self.info_label = self._label(card, "", muted=True, font="small")
        self.info_label.configure(anchor="center")
        self.info_label.grid(row=2, column=0, pady=(10, 0))

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="we", padx=22, pady=(16, 20))
        bottom.columnconfigure(0, weight=1)
        self.save_button = ctk.CTkButton(
            bottom,
            text="Save watermarked image",
            height=46,
            corner_radius=12,
            font=self.fonts["button"],
            command=self._on_save,
        )
        self.save_button.grid(row=0, column=0, sticky="we")

        self.target_label = ctk.CTkLabel(
            bottom,
            textvariable=self.target_var,
            font=self.fonts["small"],
            text_color=PALETTE["muted"],
            wraplength=self.TILE_SIZE,
        )
        self.target_label.grid(row=1, column=0, pady=(8, 0))

        # Fixed-height area for status messages, so the window does not
        # change size when a warning or the "Saved" note appears.
        messages = ctk.CTkFrame(
            bottom, height=self.MESSAGE_AREA_HEIGHT, fg_color="transparent"
        )
        messages.grid(row=2, column=0, sticky="we")
        messages.grid_propagate(False)
        messages.columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(
            messages,
            textvariable=self.status_var,
            font=self.fonts["small"],
            height=20,
            wraplength=self.TILE_SIZE,
            justify="center",
        )
        self.status_label.grid(row=0, column=0, sticky="n")
        # Link-style button, only shown once a file has been saved.
        self.reveal_button = ctk.CTkButton(
            messages,
            text="Show in Finder" if IS_MAC else "Show in folder",
            height=24,
            fg_color="transparent",
            hover_color=PALETTE["track"],
            text_color=PALETTE["accent"],
            font=self.fonts["small"],
            command=self._reveal_saved,
        )
        self.reveal_button.grid(row=1, column=0)
        self.reveal_button.grid_remove()

    # ----- reading and writing the form ----------------------------------------

    def set_image_path(self, path: "str | Path") -> None:
        """Replace the content of the image field (the preview follows).

        Args:
            path: New image path ("" to clear).
        """
        self.image_var.set(str(path))

    def _update_image_placeholder(self, *_args) -> None:
        """Show the image-field hint only while the field is empty."""
        if self.image_var.get():
            self.image_placeholder.place_forget()
        else:
            self.image_placeholder.place(
                in_=self.image_entry, x=17, rely=0.5, anchor="w"
            )

    def current_format(self) -> str:
        """Return the selected output format as a :data:`FORMATS` key.

        Returns:
            The canonical format, e.g. ``"png"``.

        Raises:
            ValueError: If the selector holds an unknown value.
        """
        return normalize_format(self.format_var.get())

    def collect_watermark(self) -> Watermark:
        """Read the text and layout fields into a validated watermark.

        Returns:
            The :class:`Watermark`.

        Raises:
            ValueError: If a field holds an invalid value.
        """
        return make_watermark(
            text=self.text_var.get(),
            font_size=self.size_var.get(),
            font=self.font_var.get(),
            color=self.color_var.get(),
            opacity=self.opacity_var.get(),
            angle=self.angle_var.get(),
            spacing=round(self.spacing_var.get(), 2),
            layout=self.layout_var.get(),
            outline=self.outline_var.get(),
        )

    def quality(self) -> int:
        """Return the JPG/WEBP quality typed into the export section.

        Returns:
            The quality (1-100); the default for lossless formats, whose
            (disabled) quality field is ignored.

        Raises:
            ValueError: If the field is not a whole number from 1 to 100.
        """
        if not FORMATS[self.current_format()].lossy:
            return DEFAULT_QUALITY
        value = _to_int(self.quality_var.get(), "Quality")
        _check_range(value, "Quality", 1, 100)
        return value

    def target_path(self) -> Path:
        """Return the file the current form would be saved to.

        A known image extension typed into the name field is dropped and
        replaced by the extension of the selected format, so the format
        selector is always the single source of truth.

        Returns:
            ``<folder>/<name>.<format>``.

        Raises:
            ValueError: If the file name is empty.
        """
        name = self.name_var.get().strip()
        if format_from_suffix(Path(name)) is not None:
            name = Path(name).stem
        if not name:
            raise ValueError("Please enter an output file name.")
        folder = Path(self.folder_var.get().strip() or ".").expanduser()
        return folder / f"{name}.{self.current_format()}"

    # ----- image loading ---------------------------------------------------------

    def _sync_source(self) -> None:
        """Open the image named in the image field if it changed.

        Raises:
            ValueError: If the named file cannot be opened.
        """
        text = clean_path_text(self.image_var.get())
        if text == self._source_text and (self.source or not text):
            return
        if self._load_error and self._load_error[0] == text:
            raise ValueError(self._load_error[1])  # don't retry on every refresh
        self.source = None
        self._preview_base = None
        self._source_text = text
        self._load_error = None
        if not text:
            return
        try:
            source = open_image(text)
        except ValueError as exc:
            self._load_error = (text, str(exc))
            raise
        self.source = source
        size = fit_size(source.image.size, self.PREVIEW_SIZE)
        self._preview_base = source.image.resize(
            size, Image.Resampling.LANCZOS, reducing_gap=3.0
        )
        self._preview_scale = size[0] / source.image.size[0]
        self._photos["original"] = ImageTk.PhotoImage(
            self._preview_base, master=self.root
        )
        self._adopt_output_fields(source)

    def _adopt_output_fields(self, source: SourceImage) -> None:
        """Point the export fields at the newly opened image.

        Fields pre-filled by the caller are kept for the first image only.

        Args:
            source: The image that was just opened.
        """
        if self._adopt["name"]:
            self.name_var.set(default_output_name(source.path))
        if self._adopt["folder"]:
            self.folder_var.set(str(source.path.parent.resolve()))
        if self._adopt["fmt"]:
            self.format_var.set((source.format or DEFAULT_FORMAT).upper())
        self._adopt = dict.fromkeys(self._adopt, True)

    # ----- live preview ----------------------------------------------------------

    def _cancel_pending_refresh(self) -> None:
        """Drop a scheduled preview refresh, if there is one."""
        if self._pending_refresh is not None:
            self.root.after_cancel(self._pending_refresh)
            self._pending_refresh = None

    def _schedule_refresh(self, *_args) -> None:
        """Debounce preview updates so typing and dragging stay responsive."""
        if self._destroyed:
            return
        self._cancel_pending_refresh()
        self._pending_refresh = self.root.after(
            self.REFRESH_DELAY_MS, self.refresh_preview
        )

    def _on_destroy(self, _event=None) -> None:
        """Stop callbacks from firing into a window that is being closed.

        Args:
            _event: The Tk ``<Destroy>`` event (unused).
        """
        self._destroyed = True
        self._cancel_pending_refresh()
        ctk.AppearanceModeTracker.remove(self._on_appearance_change)

    def refresh_preview(self) -> None:
        """Re-render the preview, hints, target path and status."""
        self._cancel_pending_refresh()  # this call makes a queued one redundant
        self._comparing = False
        self._photos.pop("watermarked", None)  # only compare a fresh preview
        self._update_swatch()
        try:
            self._sync_source()
        except ValueError as exc:
            self._update_hints()
            self._show_placeholder("Cannot open this image\nClick to choose another")
            self._show_message("", "", str(exc), "error")
            return
        self._update_hints()  # after loading: the size hint needs the image
        if self.source is None:
            self._show_placeholder("Click to open an image")
            self._show_message("PNG, JPG, WEBP, TIFF, BMP or GIF", "", "", "info")
            return

        width, height = self.source.image.size
        facts = [f"{width} × {height} px"]
        try:
            target = shorten_home(self.target_path())
        except ValueError as exc:
            target = str(exc)
        try:
            wm = self.collect_watermark()
            layer = build_layer(self._preview_base.size, wm, self._preview_scale)
        except ValueError as exc:
            self._draw_image(self._preview_base)  # the original, untouched
            self._show_message(facts[0], target, str(exc), "error")
            return

        preview = composite(self._preview_base, layer.image)
        self._photos["watermarked"] = ImageTk.PhotoImage(preview, master=self.root)
        self._draw_image(self._photos["watermarked"])
        facts.append(f"text {font_size_px(wm.font_size, (width, height))} px")
        facts.append("hold to compare")
        info = "  ·  ".join(facts)

        try:
            fmt = self.current_format()
            self.quality()
            path = self.target_path()
            if is_same_file(path, self.source.path):
                raise ValueError(
                    "This would replace the original image; choose another name."
                )
        except ValueError as exc:
            self._show_message(info, target, str(exc), "error")
            return
        warnings = watermark_warnings(
            (width, height), wm, fmt, self.source.has_transparency
        )
        self._show_message(
            info, target, " ".join(warnings), "warning" if warnings else "info", True
        )

    def _show_message(
        self, info: str, target: str, status: str, kind: str, can_save: bool = False
    ) -> None:
        """Update the texts under the preview and the save button state.

        Args:
            info: Facts shown under the preview.
            target: Output path shown under the save button.
            status: Colour-coded message ("" hides it).
            kind: ``"info"``, ``"success"``, ``"warning"`` or ``"error"``.
            can_save: Whether the form can currently be saved.
        """
        self.info_label.configure(text=info)
        self.target_var.set(target)
        self._set_status(status, kind)
        self._set_save_enabled(can_save)

    def _draw_image(self, image: "Image.Image | ImageTk.PhotoImage") -> None:
        """Show an image centred in the preview tile.

        Args:
            image: A Pillow image (converted here) or a ready PhotoImage.
        """
        if isinstance(image, Image.Image):
            image = ImageTk.PhotoImage(image, master=self.root)
            self._photos["shown"] = image
        canvas = self.preview_canvas
        canvas.delete("all")
        canvas.configure(background=self._themed(PALETTE["tile"]))
        side = self.PREVIEW_SIZE
        width, height = image.width(), image.height()
        left, top = (side - width) // 2, (side - height) // 2
        if self.source is not None and self.source.has_transparency:
            # Checkerboard behind transparent areas, inside the image only.
            square = 12
            for y in range(top, top + height, square):
                for x in range(left, left + width, square):
                    color = CHECKER_COLORS[
                        ((x - left) // square + (y - top) // square) % 2
                    ]
                    canvas.create_rectangle(
                        x,
                        y,
                        min(x + square, left + width),
                        min(y + square, top + height),
                        fill=color,
                        outline="",
                        tags="checker",
                    )
        canvas.create_image(left, top, image=image, anchor="nw", tags="image")

    def _show_placeholder(self, text: str) -> None:
        """Replace the preview with a dashed frame and a short message.

        Args:
            text: Message shown in the middle of the preview area.
        """
        canvas = self.preview_canvas
        canvas.delete("all")
        canvas.configure(background=self._themed(PALETTE["tile"]))
        side, margin = self.PREVIEW_SIZE, 34
        canvas.create_rectangle(
            margin,
            margin,
            side - margin,
            side - margin,
            outline=self._themed(PALETTE["field_border"]),
            dash=(6, 5),
            width=2,
            tags="placeholder",
        )
        canvas.create_text(
            side / 2,
            side / 2,
            text=text,
            fill=self._themed(PALETTE["muted"]),
            font=self.fonts["body"],
            justify="center",
            tags="placeholder",
        )

    def _set_status(self, text: str, kind: str = "info") -> None:
        """Show a colour-coded message under the save button.

        Args:
            text: Message to show ("" hides it).
            kind: ``"info"``, ``"success"``, ``"warning"`` or ``"error"``.
        """
        colors = {
            "info": PALETTE["muted"],
            "success": PALETTE["success"],
            "warning": PALETTE["warning"],
            "error": PALETTE["error"],
        }
        self.status_var.set(text)
        self.status_label.configure(text_color=colors[kind])
        if kind != "success":
            self.reveal_button.grid_remove()

    def _set_save_enabled(self, enabled: bool) -> None:
        """Switch the save button between its blue and greyed-out looks.

        Args:
            enabled: Whether the form can currently be saved.
        """
        if enabled:
            self.save_button.configure(
                state="normal",
                fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hover"],
                text_color="#ffffff",
            )
        else:
            self.save_button.configure(
                state="disabled",
                fg_color=PALETTE["track"],
                text_color_disabled=PALETTE["muted"],
            )

    def _update_swatch(self) -> None:
        """Paint the colour swatch, or mark it with '?' if the colour is invalid."""
        try:
            color = color_to_hex(parse_color(self.color_var.get()))
        except ValueError:
            self.color_swatch.configure(
                fg_color=PALETTE["field"], hover_color=PALETTE["field"], text="?"
            )
            return
        self.color_swatch.configure(fg_color=color, hover_color=color, text="")

    def _update_hints(self) -> None:
        """Refresh the small grey hints next to the fields."""
        try:
            self.font_hint.configure(text=font_display_name(self.font_var.get()))
        except (ValueError, OSError):
            self.font_hint.configure(text="")
        try:
            size = parse_font_size(self.size_var.get())
            if self.source is not None:
                px = font_size_px(size, self.source.image.size)
                self.size_hint.configure(text=f"= {px} px" if size.relative else "px")
            else:
                self.size_hint.configure(text="" if size.relative else "px")
        except ValueError:
            self.size_hint.configure(text="")
        self.layout_hint.configure(text=LAYOUT_HINTS.get(self.layout_var.get(), ""))
        try:
            self.extension_label.configure(text=f".{self.current_format()}")
        except ValueError:
            self.extension_label.configure(text="")

    # ----- callbacks ------------------------------------------------------------

    def _update_slider_labels(self, *_args) -> None:
        """Show the current slider values next to the sliders."""
        try:
            self.opacity_label.configure(text=f"{self.opacity_var.get()}%")
            self.angle_label.configure(text=f"{self.angle_var.get()}°")
            self.spacing_label.configure(text=f"{round(self.spacing_var.get(), 2):g}×")
        except tk.TclError:  # a variable briefly holds a non-number
            pass

    def _sync_font_controls(self, *_args) -> None:
        """Highlight the font family in use (none for a custom font file)."""
        family = self.font_var.get().strip().lower()
        label = next((k for k, v in FONT_LABELS.items() if v == family), "")
        self.font_presets.set(label)

    def _sync_size_presets(self, *_args) -> None:
        """Highlight the size preset matching the size field (if any)."""
        value = self.size_var.get().strip().replace(" ", "")
        self.size_presets.set(value if value in self.SIZE_PRESETS else "")

    def _choose_font_family(self, label: str) -> None:
        """Use one of the generic font families.

        Args:
            label: Segment label, a key of :data:`FONT_LABELS`.
        """
        self.font_var.set(FONT_LABELS[label])

    def _choose_layout(self, label: str) -> None:
        """Switch between the tiled, centred and corner layouts.

        Args:
            label: Segment label, a key of :data:`LAYOUT_LABELS`.
        """
        self.layout_var.set(LAYOUT_LABELS[label])

    def _set_slider_enabled(self, slider, label, enabled: bool) -> None:
        """Grey out a slider (and its value) that does not apply right now.

        Args:
            slider: The slider widget.
            label: Its value label.
            enabled: Whether the slider applies.
        """
        slider.configure(
            state="normal" if enabled else "disabled",
            button_color=PALETTE["accent" if enabled else "switch_off"],
            progress_color=PALETTE["accent" if enabled else "switch_off"],
        )
        label.configure(text_color=PALETTE["muted" if enabled else "switch_off"])

    def _on_layout_change(self, *_args) -> None:
        """Sync the layout selector; spacing only applies to the tiled layout."""
        layout = self.layout_var.get()
        label = next((k for k, v in LAYOUT_LABELS.items() if v == layout), "")
        self.layout_selector.set(label)
        self._set_slider_enabled(
            self.spacing_slider, self.spacing_label, layout == "tiled"
        )

    def _on_format_change(self, *_args) -> None:
        """Enable the quality field for lossy formats (JPG, WEBP) only."""
        try:
            lossy = FORMATS[self.current_format()].lossy
        except ValueError:
            return
        self.quality_entry.configure(
            state="normal" if lossy else "disabled",
            text_color=PALETTE["text" if lossy else "muted"],
        )

    def _on_appearance_change(self, _mode: str) -> None:
        """Redraw the preview after a light/dark switch.

        Args:
            _mode: New appearance mode name (unused).
        """
        self._schedule_refresh()

    def _set_appearance(self, label: str) -> None:
        """Apply the appearance chosen in the Auto/Light/Dark switch.

        Args:
            label: One of the keys of :data:`APPEARANCE_MODES`.
        """
        ctk.set_appearance_mode(APPEARANCE_MODES[label])
        self._schedule_refresh()

    def _on_preview_press(self, _event=None) -> None:
        """Click on the tile: open an image, or show the original while held.

        Args:
            _event: The Tk mouse event (unused).
        """
        if self.source is None:
            self._open_image()
        elif "watermarked" in self._photos and self.preview_canvas.find_withtag(
            "image"
        ):
            self._comparing = True
            self.preview_canvas.itemconfigure("image", image=self._photos["original"])

    def _on_preview_release(self, _event=None) -> None:
        """Mouse released: show the watermarked preview again.

        Args:
            _event: The Tk mouse event (unused).
        """
        if self._comparing:
            self._comparing = False
            self.preview_canvas.itemconfigure(
                "image", image=self._photos["watermarked"]
            )

    def _open_image(self) -> None:
        """Let the user choose the image to watermark."""
        current = clean_path_text(self.image_var.get())
        initial = Path(current).expanduser().parent if current else Path.cwd()
        path = filedialog.askopenfilename(
            title="Open image",
            initialdir=str(initial) if initial.is_dir() else str(Path.cwd()),
            filetypes=IMAGE_FILETYPES,
            parent=self.root,
        )
        if path:
            self.set_image_path(path)
            self.refresh_preview()

    def _browse_font(self) -> None:
        """Let the user choose a font file (.ttf, .otf, .ttc)."""
        path = filedialog.askopenfilename(
            title="Choose a font", filetypes=FONT_FILETYPES, parent=self.root
        )
        if path:
            self.font_var.set(path)

    def _pick_color(self) -> None:
        """Open the system colour picker and store the chosen colour."""
        try:
            initial = color_to_hex(parse_color(self.color_var.get()))
        except ValueError:
            initial = None
        _rgb, hex_color = colorchooser.askcolor(
            color=initial, title="Text colour", parent=self.root
        )
        if hex_color:  # None when the dialog was cancelled
            self.color_var.set(hex_color)

    def _browse_folder(self) -> None:
        """Let the user choose the output folder."""
        folder = filedialog.askdirectory(
            initialdir=self.folder_var.get() or str(Path.cwd()), parent=self.root
        )
        if folder:
            self.folder_var.set(folder)

    def _reveal_saved(self) -> None:
        """Show the most recently saved file in the system file manager."""
        if self._last_saved is not None:
            reveal_in_file_manager(self._last_saved)

    def save(self) -> Path:
        """Write the watermarked image described by the form (no dialogs).

        Returns:
            The path of the written file.

        Raises:
            ValueError: If no image is open or the form is invalid.
            OSError: If the file cannot be written.
        """
        self._sync_source()
        if self.source is None:
            raise ValueError("Open an image first.")
        return export_watermarked(
            self.source,
            self.target_path(),
            self.collect_watermark(),
            self.current_format(),
            self.quality(),
        )

    def _on_save(self, *_args) -> None:
        """Save handler: confirm overwrites and report the outcome."""
        if self._pending_refresh is not None:
            self.refresh_preview()  # the button state must reflect the form
        if self.save_button.cget("state") == "disabled":
            return  # e.g. Cmd+S while the form has an error
        try:
            path = self.target_path()
        except ValueError as exc:
            messagebox.showerror("Cannot save", str(exc), parent=self.root)
            return
        if path.exists() and not messagebox.askyesno(
            "Replace file?",
            f"'{path.name}' already exists in {path.parent}.\nReplace it?",
            parent=self.root,
        ):
            return
        self._set_status("Saving...")
        self.root.update_idletasks()  # large photos take a moment
        try:
            saved = self.save()
        except (ValueError, OSError) as exc:
            self._set_status("")
            messagebox.showerror("Cannot save", str(exc), parent=self.root)
            return
        self._last_saved = saved
        self._set_status(f"✓ Saved {saved.name}", kind="success")
        self.reveal_button.grid()


def run_gui(args: argparse.Namespace) -> int:
    """Open the GUI, pre-filled with the command-line options.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code (0 on normal close, 1 if no GUI is available).
    """
    if ctk is None:
        print(
            "Error: the GUI needs tkinter and customtkinter (run `uv sync`); "
            "use --headless otherwise.",
            file=sys.stderr,
        )
        return 1

    # Split a given output into folder / name / format for the form; parts
    # left out follow the opened image.
    output_dir = name = None
    fmt = args.format
    if args.output:
        output = Path(args.output).expanduser()
        if output.is_dir():
            output_dir = output.resolve()
        else:
            output_dir = output.parent.resolve()
            suffix_format = format_from_suffix(output)
            name = output.stem if suffix_format else output.name
            fmt = fmt or suffix_format

    ctk.set_appearance_mode("system")
    try:
        root = ctk.CTk()
    except tk.TclError as exc:  # e.g. no display available over SSH
        print(f"Error: cannot open a window ({exc}); use --headless.", file=sys.stderr)
        return 1

    try:
        WatermarkApp(
            root,
            image_path=args.input or "",
            output_dir=output_dir,
            name=name,
            fmt=fmt,
            text=args.text,
            font=args.font,
            font_size=args.font_size,
            color=args.color,
            opacity=args.opacity,
            angle=args.angle,
            spacing=args.spacing,
            layout=args.layout,
            outline=args.outline,
            quality=args.quality,
        )
    except ValueError as exc:  # e.g. an unknown --format
        root.destroy()
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Bring the window to the front when started from a terminal (macOS).
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Add a text watermark to an image. Opens a GUI by default; "
            "use --headless to write the file directly."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Image to watermark (required with --headless; pre-fills the GUI otherwise)",
    )
    parser.add_argument(
        "output",
        nargs="?",
        help=(
            f"Output file or folder (default: <input name>{OUTPUT_SUFFIX}"
            f".<format> next to the input)"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Write the watermarked file without opening the GUI",
    )
    parser.add_argument(
        "-t",
        "--text",
        default=DEFAULT_TEXT,
        help=f"Watermark text (default: {DEFAULT_TEXT})",
    )
    parser.add_argument(
        "--font-size",
        default=DEFAULT_FONT_SIZE,
        help=(
            "Text size in pixels (e.g. 60) or as a percentage of the image's "
            "shorter side (e.g. 5%%) (default: 5%%)"
        ),
    )
    parser.add_argument(
        "--font",
        default=DEFAULT_FONT,
        help=(
            "sans, serif, mono, or the path to a .ttf/.otf/.ttc font file "
            f"(default: {DEFAULT_FONT})"
        ),
    )
    parser.add_argument(
        "--color",
        "--colour",
        dest="color",
        default=DEFAULT_COLOR,
        help=f"Text colour: name or hex, e.g. white or '#c8c8c8' (default: {DEFAULT_COLOR})",
    )
    parser.add_argument(
        "--opacity",
        type=int,
        default=DEFAULT_OPACITY,
        help=f"Opacity percentage 0-100 (default: {DEFAULT_OPACITY})",
    )
    parser.add_argument(
        "--outline",
        action="store_true",
        help="Draw a thin contrasting outline around the text (stands out on busy images)",
    )
    parser.add_argument(
        "--layout",
        default=DEFAULT_LAYOUT,
        type=str.lower,
        choices=[*LAYOUTS, *LAYOUT_ALIASES],
        help=(
            "tiled (repeated over the image), centre (one copy in the middle) "
            f"or corner (bottom-right signature) (default: {DEFAULT_LAYOUT})"
        ),
    )
    parser.add_argument(
        "--angle",
        type=float,
        default=DEFAULT_ANGLE,
        help=(
            f"Text angle in degrees, -{MAX_ANGLE} to {MAX_ANGLE}; positive tilts "
            f"it upwards (default: {DEFAULT_ANGLE})"
        ),
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=DEFAULT_SPACING,
        help=(
            "Gap between tiled copies, in multiples of the text size "
            f"(default: {DEFAULT_SPACING:g})"
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        help=(
            f"Output format: {', '.join(FORMATS)} (default: taken from the output "
            f"extension, else the input's format, else {DEFAULT_FORMAT})"
        ),
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=DEFAULT_QUALITY,
        help=f"JPG/WEBP quality 1-100 (default: {DEFAULT_QUALITY})",
    )
    return parser


def run_headless(args: argparse.Namespace) -> int:
    """Write the watermarked image from command-line arguments.

    Args:
        args: Parsed command-line arguments (``args.input`` must be set).

    Returns:
        Process exit code: 0 on success, 1 on error.
    """
    try:
        wm = make_watermark(
            text=args.text,
            font_size=args.font_size,
            font=args.font,
            color=args.color,
            opacity=args.opacity,
            angle=args.angle,
            spacing=args.spacing,
            layout=args.layout,
            outline=args.outline,
        )
        source = open_image(args.input)
        output_path, fmt = resolve_output_path(
            args.output, source.path, args.format, source.format
        )
        for warning in watermark_warnings(
            source.image.size, wm, fmt, source.has_transparency
        ):
            print(f"Warning: {warning}", file=sys.stderr)
        saved = export_watermarked(source, output_path, wm, fmt, args.quality)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Watermarked image saved to {saved}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run either the GUI or headless mode.

    Args:
        argv: Argument list (default: ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.headless:
        if not args.input:
            parser.error("--headless requires the input image")
        return run_headless(args)
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
