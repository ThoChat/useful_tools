#!/usr/bin/env python3
r"""Generate QR codes from a link (or any text), with a GUI or headless.

By default the tool opens a small window where you can paste a link, see a
live preview of the QR code, and choose:

* the resolution (image size in pixels, plus the DPI used for printing),
* the QR colour and the background colour (or a transparent background),
* the error-correction level and the quiet-zone border,
* the output format (PNG, JPG, BMP, TIFF, WEBP, SVG or PDF),
* the output file name and folder.

With ``--headless`` the same options are given on the command line and the
file is written straight away, without opening any window (useful in scripts
or over SSH). Without ``--headless``, command-line options pre-fill the GUI.

Raster formats get crisp, whole-pixel modules: the code is drawn at the
largest integer number of pixels per module that fits, and centred on a
canvas of exactly the requested size (the leftover pixels become extra
background margin). SVG and PDF are true vector files.

Usage:
  uv run qr_code_generator.py
  uv run qr_code_generator.py "https://example.com"
  uv run qr_code_generator.py --headless "https://example.com"
  uv run qr_code_generator.py --headless "https://example.com" -o website.svg
  uv run qr_code_generator.py --headless "https://example.com" -o code \
      --format png --size 2048 --fg "#1d3557" --bg transparent
  uv run qr_code_generator.py --headless "https://example.com" -o flyer.pdf \
      --size 1200 --dpi 300 --error-correction H
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

import qrcode
import qrcode.constants
from PIL import Image, ImageColor
from qrcode.exceptions import DataOverflowError
from reportlab.pdfgen import canvas as pdf_canvas

# The GUI needs tkinter plus customtkinter (modern widgets, light/dark mode).
# Importing them tolerantly keeps headless mode usable on machines where
# Python was built without Tk.
try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox

    import customtkinter as ctk
except ImportError:  # pragma: no cover - depends on the Python build
    tk = ctk = None


class OutputFormat(NamedTuple):
    """Capabilities of one output file format.

    Attributes:
        pil_name: Pillow format name used to save raster images, or ``None``
            for the vector formats this script writes itself (SVG, PDF).
        supports_alpha: Whether the format can store transparency.
        stores_dpi: Whether Pillow can embed a DPI value in the file.
    """

    pil_name: str | None
    supports_alpha: bool
    stores_dpi: bool


# Supported output formats, keyed by their canonical file extension.
FORMATS: dict[str, OutputFormat] = {
    "png": OutputFormat("PNG", supports_alpha=True, stores_dpi=True),
    "jpg": OutputFormat("JPEG", supports_alpha=False, stores_dpi=True),
    "bmp": OutputFormat("BMP", supports_alpha=False, stores_dpi=True),
    "tiff": OutputFormat("TIFF", supports_alpha=True, stores_dpi=True),
    "webp": OutputFormat("WEBP", supports_alpha=True, stores_dpi=False),
    "svg": OutputFormat(None, supports_alpha=True, stores_dpi=False),
    "pdf": OutputFormat(None, supports_alpha=True, stores_dpi=False),
}

# Alternative spellings accepted for formats / file extensions.
FORMAT_ALIASES = {"jpeg": "jpg", "tif": "tiff"}

# QR error-correction levels: the share of the code that can be damaged (or
# covered, e.g. by a logo) while it still scans.
ERROR_CORRECTION_LEVELS = {
    "L": qrcode.constants.ERROR_CORRECT_L,  # ~7 %
    "M": qrcode.constants.ERROR_CORRECT_M,  # ~15 %
    "Q": qrcode.constants.ERROR_CORRECT_Q,  # ~25 %
    "H": qrcode.constants.ERROR_CORRECT_H,  # ~30 %
}

DEFAULT_NAME = "qrcode"
DEFAULT_FORMAT = "png"
DEFAULT_SIZE = 1024  # pixels (width = height)
DEFAULT_DPI = 300
DEFAULT_BORDER = 4  # quiet zone in modules; 4 is what the QR spec requires
DEFAULT_FOREGROUND = "#000000"
DEFAULT_BACKGROUND = "#ffffff"
DEFAULT_ERROR_CORRECTION = "M"

# Guard rails against accidental multi-gigabyte images or nonsense values.
MAX_SIZE = 10_000
MAX_DPI = 10_000
MAX_BORDER = 50

# Colour words that mean "no background at all".
TRANSPARENT_WORDS = {"transparent", "none"}

# Below this WCAG contrast ratio many phone cameras struggle to read a code.
MIN_CONTRAST_RATIO = 3.0

RGBA = tuple[int, int, int, int]
Matrix = list[list[bool]]


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


def resolve_output_path(output: str | None, fmt: str | None = None) -> tuple[Path, str]:
    """Work out the final output file path and format for headless mode.

    Rules:
        * no ``fmt``: the format is taken from the output's extension, or
          falls back to PNG;
        * an output without a known extension gets ``.<fmt>`` appended
          (``"my.code"`` becomes ``"my.code.png"``);
        * an extension that contradicts an explicit ``fmt`` is an error.

    Args:
        output: File name or path given by the user (``None`` → ``qrcode``).
        fmt: Explicit output format, or ``None`` to infer it.

    Returns:
        Tuple of (output path, canonical format key).

    Raises:
        ValueError: If the format is unknown or conflicts with the extension.
    """
    path = Path(output or DEFAULT_NAME).expanduser()
    suffix_format = format_from_suffix(path)

    if fmt is None:
        fmt = suffix_format or DEFAULT_FORMAT
    else:
        fmt = normalize_format(fmt)
        if suffix_format is not None and suffix_format != fmt:
            raise ValueError(
                f"Output name '{path.name}' does not match the requested format '{fmt}'"
            )

    if suffix_format is None:
        # Append rather than replace, so dots inside a name are preserved.
        path = path.with_name(f"{path.name}.{fmt}")
    return path, fmt


def parse_color(value: str) -> RGBA:
    """Convert a user-supplied colour into an RGBA tuple.

    Accepts anything Pillow understands (``"navy"``, ``"#1d3557"``,
    ``"#1d355780"`` with alpha, ``"rgb(29, 53, 87)"``...) plus the words
    ``"transparent"`` / ``"none"``.

    Args:
        value: The colour text.

    Returns:
        An ``(r, g, b, a)`` tuple with 0-255 channels. Transparent maps to
        fully transparent white ``(255, 255, 255, 0)``.

    Raises:
        ValueError: If the colour cannot be parsed.
    """
    text = value.strip()
    if text.lower() in TRANSPARENT_WORDS:
        return (255, 255, 255, 0)
    try:
        rgb = ImageColor.getrgb(text)
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


def scannability_warnings(foreground: RGBA, background: RGBA) -> list[str]:
    """List colour choices that are likely to make a QR code hard to scan.

    Args:
        foreground: Colour of the QR modules.
        background: Background colour. A transparent background is not
            checked because the final backdrop is unknown.

    Returns:
        Human-readable warnings (empty when the colours look fine).
    """
    if background[3] == 0:
        return []
    fg_lum = _relative_luminance(foreground)
    bg_lum = _relative_luminance(background)
    warnings = []

    ratio = (max(fg_lum, bg_lum) + 0.05) / (min(fg_lum, bg_lum) + 0.05)
    if ratio < MIN_CONTRAST_RATIO:
        warnings.append(
            f"Low contrast ({ratio:.1f}:1) between QR colour and background; "
            f"it may not scan reliably (aim for {MIN_CONTRAST_RATIO:.0f}:1 or more)."
        )
    if fg_lum > bg_lum:
        warnings.append(
            "Light code on a dark background (inverted); "
            "some scanners cannot read inverted QR codes."
        )
    return warnings


def _check_range(value: int, name: str, minimum: int, maximum: int) -> None:
    """Raise ``ValueError`` unless ``minimum <= value <= maximum``.

    Args:
        value: The number to check.
        name: Option name used in the error message.
        minimum: Smallest allowed value.
        maximum: Largest allowed value.
    """
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum} (got {value})"
        )


def validate_options(
    fmt: str, size: int, dpi: int, foreground: str, background: str
) -> tuple[str, RGBA, RGBA]:
    """Check the output options shared by the GUI and headless mode.

    Args:
        fmt: Output format (any key or alias of :data:`FORMATS`).
        size: Image width/height in pixels.
        dpi: Print resolution.
        foreground: QR colour text.
        background: Background colour text, or ``"transparent"``.

    Returns:
        Tuple of (canonical format, foreground RGBA, background RGBA).

    Raises:
        ValueError: If any option is invalid, including transparency
            requested for a format that cannot store it.
    """
    fmt = normalize_format(fmt)
    _check_range(size, "Size", 1, MAX_SIZE)
    _check_range(dpi, "DPI", 1, MAX_DPI)
    fg = parse_color(foreground)
    bg = parse_color(background)
    if fg[3] == 0:
        raise ValueError("The QR colour cannot be fully transparent.")
    if not FORMATS[fmt].supports_alpha and (fg[3] < 255 or bg[3] < 255):
        raise ValueError(
            f"{fmt.upper()} does not support transparency; pick an opaque "
            f"colour or use png, tiff, webp, svg or pdf."
        )
    return fmt, fg, bg


# --------------------------------------------------------------------------
# QR code construction and rendering
# --------------------------------------------------------------------------


def build_matrix(
    data: str,
    error_correction: str = DEFAULT_ERROR_CORRECTION,
    border: int = DEFAULT_BORDER,
) -> Matrix:
    """Encode text into a QR module matrix (``True`` = dark module).

    The smallest QR version that fits the data is chosen automatically.

    Args:
        data: Link or text to encode; surrounding whitespace is stripped
            (pasted links often carry a trailing newline).
        error_correction: One of ``"L"``, ``"M"``, ``"Q"``, ``"H"``.
        border: Quiet-zone width in modules, included in the matrix.

    Returns:
        A square list of rows of booleans, quiet zone included.

    Raises:
        ValueError: If the data is empty or too long, or an option is invalid.
    """
    text = data.strip()
    if not text:
        raise ValueError("Nothing to encode: the link/text is empty.")
    level = ERROR_CORRECTION_LEVELS.get(error_correction.strip().upper())
    if level is None:
        raise ValueError(
            f"Error correction must be one of L, M, Q, H (got '{error_correction}')"
        )
    _check_range(border, "Border", 0, MAX_BORDER)

    qr = qrcode.QRCode(error_correction=level, border=border)
    qr.add_data(text)
    try:
        qr.make(fit=True)  # fit=True picks the smallest version that holds the data
    except (DataOverflowError, ValueError) as exc:
        # Depending on how far the data overflows, qrcode raises either
        # DataOverflowError or ValueError("Invalid version (was 41...)").
        level_name = error_correction.strip().upper()
        hint = (
            "Shorten it."
            if level_name == "L"
            else "Shorten it or lower the error correction (L holds the most)."
        )
        raise ValueError(
            f"The text is too long for a QR code at error-correction level "
            f"{level_name} ({len(text.encode('utf-8'))} bytes). {hint}"
        ) from exc
    return qr.get_matrix()


def qr_version(matrix: Matrix, border: int) -> int:
    """Return the QR version (1-40) of a matrix built by :func:`build_matrix`.

    A version-``v`` symbol is ``17 + 4 * v`` modules wide, quiet zone excluded.

    Args:
        matrix: Module matrix including the quiet zone.
        border: Quiet-zone width used to build the matrix.

    Returns:
        The QR version number.
    """
    return (len(matrix) - 2 * border - 17) // 4


def dark_runs(matrix: Matrix) -> Iterator[tuple[int, int, int]]:
    """Yield horizontal runs of consecutive dark modules.

    Merging neighbouring modules into runs keeps vector output small and
    avoids hairline seams between adjacent squares in PDF/SVG viewers.

    Args:
        matrix: Module matrix (``True`` = dark).

    Yields:
        ``(x, y, length)`` tuples in module units, row by row.
    """
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                yield start, y, x - start
            else:
                x += 1


def render_raster(
    matrix: Matrix, size: int, foreground: RGBA, background: RGBA
) -> Image.Image:
    """Draw a module matrix into a square Pillow image of exactly ``size`` px.

    Every module gets the same whole number of pixels so edges stay sharp;
    any remainder is distributed evenly as extra background margin.

    Args:
        matrix: Module matrix (``True`` = dark), quiet zone included.
        size: Width and height of the resulting image in pixels.
        foreground: Colour of dark modules.
        background: Background colour (alpha 0 = transparent).

    Returns:
        An ``RGB`` image, or ``RGBA`` when either colour is not opaque.

    Raises:
        ValueError: If ``size`` is smaller than the number of modules.
    """
    modules = len(matrix)
    module_px = size // modules
    if module_px < 1:
        raise ValueError(
            f"Size {size}px is too small for this QR code: it has {modules} "
            f"modules per side, so at least {modules}px are needed."
        )

    use_alpha = foreground[3] < 255 or background[3] < 255
    mode = "RGBA" if use_alpha else "RGB"
    channels = 4 if use_alpha else 3

    # One pixel per module (255 = dark), then scaled up without smoothing.
    mask = Image.new("L", (modules, modules), 0)
    mask.putdata([255 if cell else 0 for row in matrix for cell in row])
    drawn = modules * module_px
    mask = mask.resize((drawn, drawn), Image.Resampling.NEAREST)

    image = Image.new(mode, (size, size), background[:channels])
    offset = (size - drawn) // 2
    image.paste(
        foreground[:channels], (offset, offset, offset + drawn, offset + drawn), mask
    )
    return image


def _svg_fill(color: RGBA) -> str:
    """Build the SVG ``fill`` (and ``fill-opacity``) attributes for a colour.

    Args:
        color: An ``(r, g, b, a)`` tuple.

    Returns:
        Attribute text such as ``fill="#000000"``.
    """
    attributes = f'fill="{color_to_hex(color)}"'
    if color[3] < 255:
        attributes += f' fill-opacity="{color[3] / 255:.3f}"'
    return attributes


def render_svg(matrix: Matrix, size: int, foreground: RGBA, background: RGBA) -> str:
    """Render a module matrix as a standalone SVG document.

    The drawing uses one unit per module (``viewBox``) and is displayed at
    ``size`` x ``size`` pixels; it scales losslessly to any size.

    Args:
        matrix: Module matrix (``True`` = dark), quiet zone included.
        size: Displayed width and height in pixels.
        foreground: Colour of dark modules.
        background: Background colour; omitted entirely when transparent.

    Returns:
        The SVG document as a string.
    """
    modules = len(matrix)
    path_data = "".join(f"M{x} {y}h{n}v1h-{n}z" for x, y, n in dark_runs(matrix))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {modules} {modules}" shape-rendering="crispEdges">',
    ]
    if background[3] > 0:
        lines.append(
            f'<rect width="{modules}" height="{modules}" {_svg_fill(background)}/>'
        )
    lines.append(f'<path d="{path_data}" {_svg_fill(foreground)}/>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def write_pdf(
    matrix: Matrix,
    path: Path,
    size: int,
    dpi: int,
    foreground: RGBA,
    background: RGBA,
) -> None:
    """Write a module matrix as a single-page vector PDF.

    The page is square, ``size / dpi`` inches wide, i.e. it prints at the
    same physical size as the raster image would at that DPI.

    Args:
        matrix: Module matrix (``True`` = dark), quiet zone included.
        path: Destination ``.pdf`` path.
        size: Nominal size in pixels.
        dpi: Dots per inch used to turn ``size`` into a physical page size.
        foreground: Colour of dark modules.
        background: Background colour; not painted when transparent.
    """
    side = size / dpi * 72  # PDF units are points (1/72 inch)
    module = side / len(matrix)

    pdf = pdf_canvas.Canvas(str(path), pagesize=(side, side))
    pdf.setTitle("QR code")
    pdf.setCreator("useful-tools qr_code_generator.py")

    def set_fill(color: RGBA) -> None:
        r, g, b = (c / 255 for c in color[:3])
        pdf.setFillColorRGB(r, g, b, alpha=color[3] / 255)

    if background[3] > 0:
        set_fill(background)
        pdf.rect(0, 0, side, side, stroke=0, fill=1)

    # All dark runs go into one path so viewers anti-alias it as a whole
    # (separate rectangles can show thin seams between them).
    modules_path = pdf.beginPath()
    for x, y, length in dark_runs(matrix):
        # PDF's origin is bottom-left, while matrix rows go top-down.
        modules_path.rect(x * module, side - (y + 1) * module, length * module, module)
    set_fill(foreground)
    pdf.drawPath(modules_path, stroke=0, fill=1)
    pdf.showPage()
    pdf.save()


def save_raster(image: Image.Image, path: Path, fmt: str, dpi: int) -> None:
    """Save a Pillow image with sensible per-format settings.

    Args:
        image: The rendered QR image.
        path: Destination file path.
        fmt: Canonical raster format key (``"png"``, ``"jpg"``...).
        dpi: Resolution metadata embedded where the format supports it.
    """
    spec = FORMATS[fmt]
    options: dict = {}
    if spec.stores_dpi:
        options["dpi"] = (dpi, dpi)
    if fmt == "jpg":
        # Highest quality and no chroma subsampling keep module edges clean.
        options.update(quality=95, subsampling=0)
    elif fmt == "webp":
        options["lossless"] = True
    elif fmt == "tiff":
        options["compression"] = "tiff_lzw"
    image.save(path, format=spec.pil_name, **options)


def generate_qr_code(
    data: str,
    output_path: Path,
    fmt: str = DEFAULT_FORMAT,
    size: int = DEFAULT_SIZE,
    foreground: str = DEFAULT_FOREGROUND,
    background: str = DEFAULT_BACKGROUND,
    error_correction: str = DEFAULT_ERROR_CORRECTION,
    border: int = DEFAULT_BORDER,
    dpi: int = DEFAULT_DPI,
) -> Path:
    """Validate the options, build the QR code and write it to disk.

    This is the single entry point shared by the GUI and headless mode.
    Missing parent folders of ``output_path`` are created.

    Args:
        data: Link or text to encode.
        output_path: Destination file (used as-is; no extension is added).
        fmt: Output format (any key or alias of :data:`FORMATS`).
        size: Image width/height in pixels (also sets the SVG/PDF size).
        foreground: QR colour (any Pillow colour string).
        background: Background colour, or ``"transparent"``.
        error_correction: ``"L"``, ``"M"``, ``"Q"`` or ``"H"``.
        border: Quiet-zone width in modules.
        dpi: Print resolution (raster metadata and PDF page size).

    Returns:
        The path of the written file.

    Raises:
        ValueError: If any option is invalid or the data does not fit.
        OSError: If the file cannot be written.
    """
    fmt, fg, bg = validate_options(fmt, size, dpi, foreground, background)
    matrix = build_matrix(data, error_correction, border)

    output_path = Path(output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "svg":
        output_path.write_text(render_svg(matrix, size, fg, bg), encoding="utf-8")
    elif fmt == "pdf":
        write_pdf(matrix, output_path, size, dpi, fg, bg)
    else:
        save_raster(render_raster(matrix, size, fg, bg), output_path, fmt, dpi)
    return output_path


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

# GUI colours as (light mode, dark mode) pairs. They follow Apple's system
# palette so the window feels native on macOS and still looks clean elsewhere.
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

# Light/dark squares used to show a transparent background in the preview.
CHECKER_COLORS = ("#ffffff", "#e3e3e8")

# Label of the appearance switch -> customtkinter appearance mode.
APPEARANCE_MODES = {"Auto": "system", "Light": "light", "Dark": "dark"}

# Short explanation shown next to the error-correction selector.
ERROR_CORRECTION_HINTS = {
    "L": "recovers ~7% damage",
    "M": "recovers ~15% damage",
    "Q": "recovers ~25% damage",
    "H": "recovers ~30% damage",
}

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


def _blend_hex(color: RGBA, under: RGBA) -> str:
    """Flatten a (possibly translucent) colour onto another one.

    Tk canvases cannot draw transparency, so translucent colours are
    previewed as the solid colour they produce over their backdrop.

    Args:
        color: Colour drawn on top.
        under: Opaque backdrop colour.

    Returns:
        The resulting colour as ``#rrggbb``.
    """
    alpha = color[3] / 255
    mixed = tuple(round(c * alpha + u * (1 - alpha)) for c, u in zip(color, under))
    return color_to_hex((*mixed[:3], 255))


def shorten_home(path: Path) -> str:
    """Display a path with the home folder abbreviated to ``~``.

    Args:
        path: Any path.

    Returns:
        The path as text, e.g. ``~/Desktop/qrcode.png``.
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


class QRCodeApp:
    """Modern CustomTkinter window for creating QR codes with a live preview.

    The widgets only collect options; all validation and rendering is
    delegated to the same functions used by headless mode.
    """

    PREVIEW_SIZE = 264  # side of the preview drawing area in pixels
    TILE_SIZE = 300  # side of the rounded tile around the preview
    MESSAGE_AREA_HEIGHT = 64  # room for ~3 lines of status text
    FIELD_HEIGHT = 32
    REFRESH_DELAY_MS = 150  # debounce delay between typing and re-rendering
    SIZE_PRESETS = ["512", "1024", "2048", "4096"]

    def __init__(
        self,
        root: "ctk.CTk",
        data: str = "",
        output_dir: Path | None = None,
        name: str = DEFAULT_NAME,
        fmt: str = DEFAULT_FORMAT,
        size: int = DEFAULT_SIZE,
        dpi: int = DEFAULT_DPI,
        foreground: str = DEFAULT_FOREGROUND,
        background: str = DEFAULT_BACKGROUND,
        error_correction: str = DEFAULT_ERROR_CORRECTION,
        border: int = DEFAULT_BORDER,
    ):
        """Build the window and pre-fill it with the given options.

        Args:
            root: The CustomTkinter root window to populate.
            data: Initial link/text.
            output_dir: Initial output folder (default: current directory).
            name: Initial output file name (without extension).
            fmt: Initial output format.
            size: Initial image size in pixels.
            dpi: Initial DPI.
            foreground: Initial QR colour.
            background: Initial background colour, or ``"transparent"``.
            error_correction: Initial error-correction level.
            border: Initial quiet-zone width in modules.

        Raises:
            ValueError: If ``fmt`` or ``error_correction`` is unknown.
        """
        self.root = root
        root.title("QR Code Generator")
        root.configure(fg_color=PALETTE["window"])

        transparent = background.strip().lower() in TRANSPARENT_WORDS
        level = error_correction.strip().upper()
        if level not in ERROR_CORRECTION_LEVELS:
            raise ValueError(
                f"Error correction must be one of L, M, Q, H (got '{level}')"
            )

        # Tk variables backing every input field.
        self.data_var = tk.StringVar(value=data)
        self.size_var = tk.StringVar(value=str(size))
        self.dpi_var = tk.StringVar(value=str(dpi))
        self.ec_var = tk.StringVar(value=level)
        self.border_var = tk.StringVar(value=str(border))
        self.fg_var = tk.StringVar(value=foreground)
        self.bg_var = tk.StringVar(
            value=DEFAULT_BACKGROUND if transparent else background
        )
        self.transparent_var = tk.BooleanVar(value=transparent)
        self.format_var = tk.StringVar(value=normalize_format(fmt).upper())
        self.name_var = tk.StringVar(value=name)
        self.folder_var = tk.StringVar(value=str(output_dir or Path.cwd()))
        self.appearance_var = tk.StringVar(value="Auto")
        self.target_var = tk.StringVar()
        self.status_var = tk.StringVar()

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
            self.data_var,
            self.size_var,
            self.dpi_var,
            self.ec_var,
            self.border_var,
            self.fg_var,
            self.bg_var,
            self.transparent_var,
            self.format_var,
            self.name_var,
            self.folder_var,
        ):
            var.trace_add("write", self._schedule_refresh)
        self.data_var.trace_add("write", self._update_link_placeholder)
        self.size_var.trace_add("write", self._sync_size_presets)
        self.format_var.trace_add("write", self._on_format_change)
        self.transparent_var.trace_add("write", self._on_transparency_change)

        # Redraw the (manually coloured) preview canvas on light/dark switches.
        ctk.AppearanceModeTracker.add(self._on_appearance_change, root)
        self.preview_canvas.bind("<Destroy>", self._on_destroy)

        self._update_link_placeholder()
        self._sync_size_presets()
        self._on_format_change()
        self._on_transparency_change()
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

    def _label(self, parent, text: str, muted: bool = False, font: str = "body"):
        """Create a text label.

        Args:
            parent: Parent widget.
            text: Label text.
            muted: Use the secondary (grey) text colour.
            font: Key of :attr:`fonts`.

        Returns:
            The label widget.
        """
        return ctk.CTkLabel(
            parent,
            text=text,
            font=self.fonts[font],
            text_color=PALETTE["muted" if muted else "text"],
            anchor="w",
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
        self._build_link_bar(main)

        cards = ctk.CTkFrame(main, fg_color="transparent")
        cards.grid(row=2, column=0, sticky="nsew")
        cards.columnconfigure(0, weight=1)
        cards.rowconfigure(0, weight=1)
        self._build_settings_card(cards)
        self._build_preview_card(cards)

        # Keyboard shortcuts: Return in the link field or Cmd/Ctrl+S saves.
        self.data_entry.bind("<Return>", self._on_save)
        self.root.bind("<Command-s>", self._on_save)
        self.root.bind("<Control-s>", self._on_save)
        self.data_entry.focus_set()

    def _build_header(self, parent) -> None:
        """Create the title, subtitle and the light/dark appearance switch.

        Args:
            parent: Frame the header is placed in.
        """
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="we", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        self._label(header, "QR Code Generator", font="title").grid(
            row=0, column=0, sticky="w"
        )
        self._label(
            header,
            "Paste a link, style it, and export it as an image or vector file.",
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

    def _build_link_bar(self, parent) -> None:
        """Create the large link field with its Paste / Clear buttons.

        Args:
            parent: Frame the link bar is placed in.
        """
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="we", pady=(0, 18))
        bar.columnconfigure(0, weight=1)

        self.data_entry = ctk.CTkEntry(
            bar,
            textvariable=self.data_var,
            height=48,
            corner_radius=12,
            border_width=1,
            fg_color=PALETTE["card"],
            border_color=PALETTE["field_border"],
            text_color=PALETTE["text"],
            font=self.fonts["link"],
        )
        self.data_entry.grid(row=0, column=0, sticky="we")

        # CTkEntry hides its placeholder while focused, but the field is
        # focused on start-up (so Cmd/Ctrl+V works at once). A label laid
        # over the field keeps the hint visible until something is typed,
        # like native macOS text fields. It starts just right of the caret.
        self.link_placeholder = ctk.CTkLabel(
            bar,
            text="Paste a link or type any text...",
            font=self.fonts["link"],
            text_color=PALETTE["muted"],
            fg_color=PALETTE["card"],
            height=24,
            anchor="w",
        )
        self.link_placeholder.place(in_=self.data_entry, x=17, rely=0.5, anchor="w")
        self.link_placeholder.bind("<Button-1>", lambda _e: self.data_entry.focus_set())

        self._secondary_button(
            bar, "Paste", self._paste, width=88, height=48, corner_radius=12
        ).grid(row=0, column=1, padx=(10, 0))
        self._secondary_button(
            bar,
            "Clear",
            lambda: self.set_data(""),
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

    def _color_controls(self, parent, var, title: str):
        """Create a clickable colour swatch followed by a hex field.

        Args:
            parent: Row frame (pack layout).
            var: Variable holding the colour text.
            title: Title of the colour-picker dialog.

        Returns:
            The (swatch button, entry) widgets.
        """
        swatch = ctk.CTkButton(
            parent,
            text="",
            width=self.FIELD_HEIGHT,
            height=self.FIELD_HEIGHT,
            corner_radius=8,
            border_width=1,
            border_color=PALETTE["field_border"],
            text_color=PALETTE["error"],
            font=self.fonts["body"],
            command=lambda: self._pick_color(var, title),
        )
        swatch.pack(side="left")
        entry = self._entry(parent, var, width=96, mono=True)
        entry.pack(side="left", padx=(8, 0))
        return swatch, entry

    def _build_settings_card(self, parent) -> None:
        """Create the settings card (style, resolution, error correction, export).

        Args:
            parent: Frame the card is placed in.
        """
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        card.columnconfigure(1, weight=1)

        self._section(card, 0, "Style", first=True)
        row = self._field_row(card, 1, "QR colour")
        self.fg_swatch, self.fg_entry = self._color_controls(
            row, self.fg_var, "QR colour"
        )
        row = self._field_row(card, 2, "Background")
        self.bg_swatch, self.bg_entry = self._color_controls(
            row, self.bg_var, "Background colour"
        )
        self.transparent_switch = ctk.CTkSwitch(
            row,
            text="Transparent",
            variable=self.transparent_var,
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
        self.transparent_switch.pack(side="left", padx=(18, 0))

        self._section(card, 3, "Resolution")
        row = self._field_row(card, 4, "Size")
        self.size_presets = self._segmented(
            row, self.SIZE_PRESETS, width=236, command=self.size_var.set
        )
        self.size_presets.pack(side="left")
        self._entry(row, self.size_var, width=64).pack(side="left", padx=(10, 6))
        self._label(row, "px", muted=True).pack(side="left")

        row = self._field_row(card, 5, "Print DPI")
        self._entry(row, self.dpi_var, width=64).pack(side="left")
        self._label(row, "Border").pack(side="left", padx=(22, 10))
        self._entry(row, self.border_var, width=48).pack(side="left")
        self._label(row, "modules", muted=True).pack(side="left", padx=(6, 0))

        self._section(card, 6, "Error correction")
        row = self._field_row(card, 7, "Level")
        self._segmented(
            row, list(ERROR_CORRECTION_LEVELS), width=176, variable=self.ec_var
        ).pack(side="left")
        self.ec_hint = self._label(row, "", muted=True, font="small")
        self.ec_hint.pack(side="left", padx=(12, 0))

        self._section(card, 8, "Export")
        row = self._field_row(card, 9, "Format")
        self._segmented(
            row, [f.upper() for f in FORMATS], width=364, variable=self.format_var
        ).pack(side="left")
        row = self._field_row(card, 10, "File name")
        self._entry(row, self.name_var).pack(side="left", fill="x", expand=True)
        self.extension_label = self._label(row, "", muted=True)
        self.extension_label.pack(side="left", padx=(8, 0))
        row = self._field_row(card, 11, "Folder")
        self._entry(row, self.folder_var).pack(side="left", fill="x", expand=True)
        self._secondary_button(row, "Browse", self._browse_folder).pack(
            side="left", padx=(8, 0)
        )

        # Bottom padding of the card (an empty row with a minimum height).
        card.rowconfigure(12, minsize=16)

    def _build_preview_card(self, parent) -> None:
        """Create the preview card with the QR tile and the save button.

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
        )
        self.preview_canvas.place(relx=0.5, rely=0.5, anchor="center")

        self.info_label = self._label(card, "", muted=True, font="small")
        self.info_label.configure(anchor="center")
        self.info_label.grid(row=2, column=0, pady=(10, 0))

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="we", padx=22, pady=(16, 20))
        bottom.columnconfigure(0, weight=1)
        self.save_button = ctk.CTkButton(
            bottom,
            text="Save QR code",
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

    def get_data(self) -> str:
        """Return the link/text typed into the link field.

        Returns:
            The field content.
        """
        return self.data_var.get()

    def set_data(self, text: str) -> None:
        """Replace the content of the link field (the preview follows).

        Args:
            text: New link/text.
        """
        self.data_var.set(text)

    def _update_link_placeholder(self, *_args) -> None:
        """Show the link hint only while the link field is empty."""
        if self.data_var.get():
            self.link_placeholder.place_forget()
        else:
            self.link_placeholder.place(in_=self.data_entry, x=17, rely=0.5, anchor="w")

    def current_format(self) -> str:
        """Return the selected output format as a :data:`FORMATS` key.

        Returns:
            The canonical format, e.g. ``"png"``.

        Raises:
            ValueError: If the selector holds an unknown value.
        """
        return normalize_format(self.format_var.get())

    def collect_options(self) -> dict:
        """Read the form into keyword arguments for :func:`generate_qr_code`.

        Returns:
            A dict with ``data``, ``fmt``, ``size``, ``dpi``, ``foreground``,
            ``background``, ``error_correction`` and ``border``.

        Raises:
            ValueError: If a field holds an invalid value.
        """
        return {
            "data": self.get_data(),
            "fmt": self.current_format(),
            "size": _to_int(self.size_var.get(), "Size"),
            "dpi": _to_int(self.dpi_var.get(), "DPI"),
            "foreground": self.fg_var.get(),
            "background": "transparent"
            if self.transparent_var.get()
            else self.bg_var.get(),
            "error_correction": self.ec_var.get(),
            "border": _to_int(self.border_var.get(), "Border"),
        }

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

    # ----- live preview ----------------------------------------------------------

    def _cancel_pending_refresh(self) -> None:
        """Drop a scheduled preview refresh, if there is one."""
        if self._pending_refresh is not None:
            self.root.after_cancel(self._pending_refresh)
            self._pending_refresh = None

    def _schedule_refresh(self, *_args) -> None:
        """Debounce preview updates so typing stays responsive."""
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
        """Re-render the preview, swatches, hints, target path and status."""
        self._cancel_pending_refresh()  # this call makes a queued one redundant
        self._update_swatch(self.fg_swatch, self.fg_var.get())
        self._update_swatch(self.bg_swatch, self.bg_var.get())
        self.ec_hint.configure(text=ERROR_CORRECTION_HINTS.get(self.ec_var.get(), ""))
        try:
            self.extension_label.configure(text=f".{self.current_format()}")
            self.target_var.set(shorten_home(self.target_path()))
        except ValueError as exc:
            self.target_var.set(str(exc))

        try:
            options = self.collect_options()
            fmt, fg, bg = validate_options(
                options["fmt"],
                options["size"],
                options["dpi"],
                options["foreground"],
                options["background"],
            )
            if not options["data"].strip():
                self._show_placeholder("Your QR code\nwill appear here")
                self.info_label.configure(text="Paste a link above to get started")
                self._set_status("")
                self._set_save_enabled(False)
                return
            matrix = build_matrix(
                options["data"], options["error_correction"], options["border"]
            )
            modules = len(matrix)
            if FORMATS[fmt].pil_name is not None and options["size"] < modules:
                raise ValueError(
                    f"Size too small for this code: at least {modules} px needed."
                )
        except ValueError as exc:
            self._show_placeholder("No preview")
            self.info_label.configure(text="")
            self._set_status(str(exc), kind="error")
            self._set_save_enabled(False)
            return

        self._draw_matrix(matrix, fg, bg)
        facts = [
            f"Version {qr_version(matrix, options['border'])}",
            f"{modules} × {modules} modules",
        ]
        if FORMATS[fmt].pil_name is not None:
            facts.append(f"{options['size'] // modules} px/module")
        else:
            facts.append("vector")
        self.info_label.configure(text="  ·  ".join(facts))

        warnings = scannability_warnings(fg, bg)
        self._set_status(" ".join(warnings), kind="warning" if warnings else "info")
        self._set_save_enabled(True)

    def _draw_matrix(self, matrix: Matrix, fg: RGBA, bg: RGBA) -> None:
        """Draw a QR matrix as vector rectangles (crisp on HiDPI screens).

        Args:
            matrix: Module matrix to display.
            fg: QR colour.
            bg: Background colour (transparent shows a checkerboard).
        """
        canvas = self.preview_canvas
        canvas.delete("all")
        side = self.PREVIEW_SIZE
        modules = len(matrix)
        # Whole pixels per module when possible, like the saved raster image.
        module = side // modules if modules <= side else side / modules
        offset = (side - modules * module) / 2

        backdrop = parse_color(CHECKER_COLORS[0])
        if bg[3] < 255:
            square = 12
            for y in range(0, side, square):
                for x in range(0, side, square):
                    color = CHECKER_COLORS[(x // square + y // square) % 2]
                    canvas.create_rectangle(
                        x,
                        y,
                        x + square,
                        y + square,
                        fill=color,
                        outline="",
                        tags="checker",
                    )
        if bg[3] > 0:
            backdrop = parse_color(_blend_hex(bg, backdrop))
            canvas.create_rectangle(
                0,
                0,
                side,
                side,
                fill=color_to_hex(backdrop),
                outline="",
                tags="background",
            )

        fill = _blend_hex(fg, backdrop)
        for x, y, length in dark_runs(matrix):
            x0, y0 = offset + x * module, offset + y * module
            canvas.create_rectangle(
                x0,
                y0,
                x0 + length * module,
                y0 + module,
                fill=fill,
                outline="",
                tags="module",
            )

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

    def _update_swatch(self, swatch, value: str) -> None:
        """Paint a colour swatch, or mark it with '?' if the colour is invalid.

        Args:
            swatch: The swatch button.
            value: Colour text from the matching entry.
        """
        try:
            color = color_to_hex(parse_color(value))
        except ValueError:
            swatch.configure(
                fg_color=PALETTE["field"], hover_color=PALETTE["field"], text="?"
            )
            return
        swatch.configure(fg_color=color, hover_color=color, text="")

    # ----- callbacks ------------------------------------------------------------

    def _sync_size_presets(self, *_args) -> None:
        """Highlight the size preset matching the size field (if any)."""
        value = self.size_var.get().strip()
        self.size_presets.set(value if value in self.SIZE_PRESETS else "")

    def _on_format_change(self, *_args) -> None:
        """Disable the transparency switch for formats that cannot store it."""
        try:
            supports_alpha = FORMATS[self.current_format()].supports_alpha
        except ValueError:
            return
        if not supports_alpha:
            self.transparent_var.set(False)
        self.transparent_switch.configure(
            state="normal" if supports_alpha else "disabled"
        )

    def _on_transparency_change(self, *_args) -> None:
        """Grey out the background colour controls while it is transparent."""
        transparent = self.transparent_var.get()
        state = "disabled" if transparent else "normal"
        self.bg_entry.configure(
            state=state, text_color=PALETTE["muted" if transparent else "text"]
        )
        self.bg_swatch.configure(state=state)

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

    def _paste(self) -> None:
        """Replace the link field with the clipboard content."""
        try:
            self.set_data(self.root.clipboard_get().strip())
        except tk.TclError:
            self._set_status(
                "The clipboard is empty or does not contain text.", "error"
            )

    def _pick_color(self, var, title: str) -> None:
        """Open the system colour picker and store the chosen colour.

        Args:
            var: Variable that receives the colour as ``#rrggbb``.
            title: Dialog title.
        """
        try:
            initial = color_to_hex(parse_color(var.get()))
        except ValueError:
            initial = None
        _rgb, hex_color = colorchooser.askcolor(
            color=initial, title=title, parent=self.root
        )
        if hex_color:  # None when the dialog was cancelled
            var.set(hex_color)

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
        """Write the QR code described by the form (no dialogs).

        Returns:
            The path of the written file.

        Raises:
            ValueError: If the form is invalid.
            OSError: If the file cannot be written.
        """
        return generate_qr_code(
            output_path=self.target_path(), **self.collect_options()
        )

    def _on_save(self, *_args) -> None:
        """Save handler: confirm overwrites and report the outcome."""
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
        try:
            saved = self.save()
        except (ValueError, OSError) as exc:
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

    # Split a given --output into folder / name / format for the form.
    output = Path(args.output).expanduser() if args.output else Path(DEFAULT_NAME)
    fmt = args.format or format_from_suffix(output) or DEFAULT_FORMAT
    name = output.stem if format_from_suffix(output) else output.name

    ctk.set_appearance_mode("system")
    try:
        root = ctk.CTk()
    except tk.TclError as exc:  # e.g. no display available over SSH
        print(f"Error: cannot open a window ({exc}); use --headless.", file=sys.stderr)
        return 1

    try:
        QRCodeApp(
            root,
            data=args.data or "",
            output_dir=output.parent.resolve(),
            name=name,
            fmt=fmt,
            size=args.size,
            dpi=args.dpi,
            foreground=args.foreground,
            background=args.background,
            error_correction=args.error_correction,
            border=args.border,
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
            "Generate a QR code from a link or text. Opens a GUI by default; "
            "use --headless to write the file directly."
        )
    )
    parser.add_argument(
        "data",
        nargs="?",
        help=(
            "Link or text to encode (required with --headless; "
            "pre-fills the GUI otherwise)"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Generate the file without opening the GUI",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            f"Output file name or path "
            f"(default: {DEFAULT_NAME}.<format> in the current folder)"
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        help=(
            f"Output format: {', '.join(FORMATS)} (default: taken from the "
            f"--output extension, else {DEFAULT_FORMAT})"
        ),
    )
    parser.add_argument(
        "-s",
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        help=f"Image width/height in pixels (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=(
            "Print resolution stored in raster files; also sets the PDF page "
            f"size (size / dpi inches) (default: {DEFAULT_DPI})"
        ),
    )
    parser.add_argument(
        "--fg",
        "--color",
        dest="foreground",
        default=DEFAULT_FOREGROUND,
        help=(
            f"QR colour: name or hex, e.g. navy or '#1d3557' "
            f"(default: {DEFAULT_FOREGROUND})"
        ),
    )
    parser.add_argument(
        "--bg",
        "--background",
        dest="background",
        default=DEFAULT_BACKGROUND,
        help=f"Background colour, or 'transparent' (default: {DEFAULT_BACKGROUND})",
    )
    parser.add_argument(
        "-e",
        "--error-correction",
        default=DEFAULT_ERROR_CORRECTION,
        choices=list(ERROR_CORRECTION_LEVELS),
        type=str.upper,
        help=f"Error-correction level L/M/Q/H (default: {DEFAULT_ERROR_CORRECTION})",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=DEFAULT_BORDER,
        help=f"Quiet-zone width in modules (default: {DEFAULT_BORDER})",
    )
    return parser


def run_headless(args: argparse.Namespace) -> int:
    """Generate the QR code file from command-line arguments.

    Args:
        args: Parsed command-line arguments (``args.data`` must be set).

    Returns:
        Process exit code: 0 on success, 1 on error.
    """
    try:
        output_path, fmt = resolve_output_path(args.output, args.format)
        for warning in scannability_warnings(
            parse_color(args.foreground), parse_color(args.background)
        ):
            print(f"Warning: {warning}", file=sys.stderr)
        saved = generate_qr_code(
            data=args.data,
            output_path=output_path,
            fmt=fmt,
            size=args.size,
            foreground=args.foreground,
            background=args.background,
            error_correction=args.error_correction,
            border=args.border,
            dpi=args.dpi,
        )
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved QR code -> {saved} ({fmt}, {args.size}px)")
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
        if not args.data or not args.data.strip():
            parser.error("--headless requires the link/text to encode")
        return run_headless(args)
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
