"""Unit tests for ``qr_code_generator.py``.

Run from the repository root:
  uv run python -m unittest discover -s tests -v

The round-trip tests decode the generated files with OpenCV's QR detector.
OpenCV is not a project dependency; pull it in just for the test run:
  uv run --with opencv-python-headless python -m unittest discover -s tests -v
Without it those tests are skipped. GUI tests are skipped when no display
(or no tkinter) is available.
"""

import contextlib
import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf as fitz
from PIL import Image
from pypdf import PdfReader

# The tool is a standalone script in the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qr_code_generator as qr  # noqa: E402

try:  # optional test-only dependencies (numpy comes with opencv)
    import cv2
    import numpy as np
except ImportError:
    cv2 = None

BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)
TRANSPARENT = (255, 255, 255, 0)
URL = "https://example.com/some/path?with=query&and=more"


def decode_image(image: Image.Image) -> str:
    """Decode a QR code from a Pillow image with OpenCV (transparency → white)."""
    rgba = image.convert("RGBA")
    flat = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba).convert(
        "RGB"
    )
    text, _points, _straight = cv2.QRCodeDetector().detectAndDecode(np.array(flat))
    return text


def rasterize_document(path: Path) -> Image.Image:
    """Render the first page of a PDF or SVG file to a Pillow image."""
    with fitz.open(path) as document:
        pixmap = document[0].get_pixmap(dpi=150)
        return Image.open(io.BytesIO(pixmap.tobytes("png")))


class TempDirTestCase(unittest.TestCase):
    """Base class providing a fresh temporary directory per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestFormatsAndPaths(unittest.TestCase):
    """Format names and output-path resolution."""

    def test_normalize_format_aliases_and_case(self):
        self.assertEqual(qr.normalize_format("PNG"), "png")
        self.assertEqual(qr.normalize_format(".jpeg"), "jpg")
        self.assertEqual(qr.normalize_format("tif"), "tiff")
        with self.assertRaises(ValueError):
            qr.normalize_format("gif")

    def test_format_from_suffix(self):
        self.assertEqual(qr.format_from_suffix(Path("a.SVG")), "svg")
        self.assertIsNone(qr.format_from_suffix(Path("a")))
        self.assertIsNone(qr.format_from_suffix(Path("a.unknown")))

    def test_resolve_default(self):
        self.assertEqual(qr.resolve_output_path(None), (Path("qrcode.png"), "png"))

    def test_resolve_infers_format_from_extension(self):
        self.assertEqual(
            qr.resolve_output_path("out/code.pdf"), (Path("out/code.pdf"), "pdf")
        )
        self.assertEqual(
            qr.resolve_output_path("code.jpeg"), (Path("code.jpeg"), "jpg")
        )

    def test_resolve_appends_extension(self):
        self.assertEqual(
            qr.resolve_output_path("code", "svg"), (Path("code.svg"), "svg")
        )
        # A dot inside the name is kept, not treated as an extension.
        self.assertEqual(
            qr.resolve_output_path("v1.2", "png"), (Path("v1.2.png"), "png")
        )

    def test_resolve_matching_explicit_format(self):
        self.assertEqual(
            qr.resolve_output_path("code.png", "PNG"), (Path("code.png"), "png")
        )

    def test_resolve_conflicting_extension_is_error(self):
        with self.assertRaises(ValueError):
            qr.resolve_output_path("code.png", "svg")


class TestColors(unittest.TestCase):
    """Colour parsing and scannability warnings."""

    def test_parse_named_and_hex(self):
        self.assertEqual(qr.parse_color("black"), BLACK)
        self.assertEqual(qr.parse_color(" #1d3557 "), (29, 53, 87, 255))
        self.assertEqual(qr.parse_color("#ff000080"), (255, 0, 0, 128))
        self.assertEqual(qr.parse_color("rgb(1, 2, 3)"), (1, 2, 3, 255))

    def test_parse_transparent(self):
        self.assertEqual(qr.parse_color("transparent"), TRANSPARENT)
        self.assertEqual(qr.parse_color("None"), TRANSPARENT)

    def test_parse_invalid(self):
        for value in ("notacolour", "#12", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qr.parse_color(value)

    def test_color_to_hex(self):
        self.assertEqual(qr.color_to_hex((29, 53, 87, 10)), "#1d3557")

    def test_no_warning_for_black_on_white(self):
        self.assertEqual(qr.scannability_warnings(BLACK, WHITE), [])
        self.assertEqual(qr.scannability_warnings(qr.parse_color("navy"), WHITE), [])

    def test_warns_on_low_contrast(self):
        warnings = qr.scannability_warnings(
            qr.parse_color("#999999"), qr.parse_color("#aaaaaa")
        )
        self.assertTrue(any("contrast" in w.lower() for w in warnings))

    def test_warns_on_inverted_code(self):
        warnings = qr.scannability_warnings(WHITE, BLACK)
        self.assertEqual(len(warnings), 1)
        self.assertIn("inverted", warnings[0])

    def test_transparent_background_not_checked(self):
        self.assertEqual(qr.scannability_warnings(WHITE, TRANSPARENT), [])


class TestValidateOptions(unittest.TestCase):
    """Shared option validation."""

    def test_valid(self):
        fmt, fg, bg = qr.validate_options("JPEG", 512, 300, "red", "white")
        self.assertEqual((fmt, fg, bg), ("jpg", (255, 0, 0, 255), WHITE))

    def test_out_of_range_numbers(self):
        for size, dpi in (
            (0, 300),
            (qr.MAX_SIZE + 1, 300),
            (512, 0),
            (512, qr.MAX_DPI + 1),
        ):
            with self.subTest(size=size, dpi=dpi), self.assertRaises(ValueError):
                qr.validate_options("png", size, dpi, "black", "white")

    def test_transparency_rejected_for_opaque_formats(self):
        for fmt in ("jpg", "bmp"):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                qr.validate_options(fmt, 512, 300, "black", "transparent")
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                qr.validate_options(fmt, 512, 300, "#00000080", "white")

    def test_transparency_accepted_for_alpha_formats(self):
        for fmt in ("png", "tiff", "webp", "svg", "pdf"):
            with self.subTest(fmt=fmt):
                qr.validate_options(fmt, 512, 300, "black", "transparent")

    def test_invisible_foreground_rejected(self):
        with self.assertRaises(ValueError):
            qr.validate_options("png", 512, 300, "transparent", "white")


class TestMatrix(unittest.TestCase):
    """QR matrix construction."""

    def test_version_1_with_border(self):
        matrix = qr.build_matrix("hi", "L", border=4)
        self.assertEqual(len(matrix), 21 + 2 * 4)
        self.assertTrue(all(len(row) == len(matrix) for row in matrix))
        self.assertEqual(qr.qr_version(matrix, 4), 1)

    def test_quiet_zone_is_light_and_finder_pattern_present(self):
        border = 4
        matrix = qr.build_matrix("hi", border=border)
        n = len(matrix)
        for i in range(n):
            for b in range(border):
                self.assertFalse(matrix[b][i] or matrix[n - 1 - b][i])
                self.assertFalse(matrix[i][b] or matrix[i][n - 1 - b])
        # Top-left finder pattern: 7x7 dark ring with a dark 3x3 centre.
        top_left = [row[border : border + 7] for row in matrix[border : border + 7]]
        self.assertTrue(all(top_left[0]) and all(top_left[6]))
        self.assertFalse(top_left[1][1])
        self.assertTrue(top_left[3][3])

    def test_zero_border(self):
        self.assertEqual(len(qr.build_matrix("hi", border=0)), 21)

    def test_input_is_stripped(self):
        self.assertEqual(qr.build_matrix("  hi\n"), qr.build_matrix("hi"))

    def test_higher_error_correction_needs_bigger_code(self):
        data = "https://example.com/a/fairly/long/path/to/force/a/bigger/version"
        low = qr.build_matrix(data, "L")
        high = qr.build_matrix(data, "h")  # lower case accepted
        self.assertGreater(len(high), len(low))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            qr.build_matrix("   ")
        with self.assertRaises(ValueError):
            qr.build_matrix("hi", "X")
        with self.assertRaises(ValueError):
            qr.build_matrix("hi", border=-1)

    def test_data_too_long(self):
        with self.assertRaises(ValueError) as ctx:
            qr.build_matrix("x" * 5000, "H")
        self.assertIn("too long", str(ctx.exception))

    def test_dark_runs(self):
        matrix = [
            [True, True, False, True],
            [False, False, False, False],
            [False, True, True, True],
        ]
        self.assertEqual(list(qr.dark_runs(matrix)), [(0, 0, 2), (3, 0, 1), (1, 2, 3)])

    def test_dark_runs_cover_every_dark_module(self):
        matrix = qr.build_matrix(URL)
        covered = {(x + i, y) for x, y, n in qr.dark_runs(matrix) for i in range(n)}
        expected = {
            (x, y) for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell
        }
        self.assertEqual(covered, expected)


class TestRendering(unittest.TestCase):
    """Raster and SVG rendering."""

    def test_raster_exact_size_and_module_fidelity(self):
        matrix = qr.build_matrix(URL)
        n = len(matrix)
        size = 500  # deliberately not a multiple of n
        image = qr.render_raster(matrix, size, BLACK, WHITE)
        self.assertEqual(image.size, (size, size))
        self.assertEqual(image.mode, "RGB")

        module_px = size // n
        offset = (size - n * module_px) // 2
        for y in range(n):
            for x in range(n):
                centre = (
                    offset + x * module_px + module_px // 2,
                    offset + y * module_px + module_px // 2,
                )
                expected = (0, 0, 0) if matrix[y][x] else (255, 255, 255)
                self.assertEqual(image.getpixel(centre), expected, f"module {x},{y}")
        # The leftover margin is background.
        self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(image.getpixel((size - 1, size - 1)), (255, 255, 255))

    def test_raster_custom_colors(self):
        matrix = qr.build_matrix("hi")
        fg, bg = (29, 53, 87, 255), (255, 240, 200, 255)
        colors = {c for _count, c in qr.render_raster(matrix, 290, fg, bg).getcolors()}
        self.assertEqual(colors, {fg[:3], bg[:3]})

    def test_raster_transparent_background(self):
        image = qr.render_raster(qr.build_matrix("hi"), 290, BLACK, TRANSPARENT)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertIn(BLACK, {c for _count, c in image.getcolors()})

    def test_raster_too_small(self):
        matrix = qr.build_matrix("hi")  # 29 modules with the border
        self.assertEqual(qr.render_raster(matrix, 29, BLACK, WHITE).size, (29, 29))
        with self.assertRaises(ValueError):
            qr.render_raster(matrix, 28, BLACK, WHITE)

    def test_svg_structure(self):
        matrix = qr.build_matrix("hi")
        n = len(matrix)
        svg = qr.render_svg(matrix, 640, (29, 53, 87, 255), WHITE)
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        self.assertEqual(root.get("width"), "640")
        self.assertEqual(root.get("viewBox"), f"0 0 {n} {n}")
        self.assertEqual(root.find(f"{ns}rect").get("fill"), "#ffffff")
        path = root.find(f"{ns}path")
        self.assertEqual(path.get("fill"), "#1d3557")
        self.assertIsNone(path.get("fill-opacity"))

    def test_svg_transparency(self):
        svg = qr.render_svg(qr.build_matrix("hi"), 100, (0, 0, 0, 128), TRANSPARENT)
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        self.assertIsNone(root.find(f"{ns}rect"))  # no background painted
        self.assertEqual(root.find(f"{ns}path").get("fill-opacity"), "0.502")


class TestGenerateFiles(TempDirTestCase):
    """End-to-end file generation."""

    def test_raster_formats(self):
        for fmt in ("png", "jpg", "bmp", "tiff", "webp"):
            with self.subTest(fmt=fmt):
                path = qr.generate_qr_code(
                    URL, self.tmp / f"code.{fmt}", fmt=fmt, size=333
                )
                with Image.open(path) as image:
                    self.assertEqual(image.size, (333, 333))
                    self.assertEqual(image.format, qr.FORMATS[fmt].pil_name)

    def test_dpi_metadata(self):
        path = qr.generate_qr_code(URL, self.tmp / "code.png", size=600, dpi=600)
        with Image.open(path) as image:
            self.assertAlmostEqual(image.info["dpi"][0], 600, places=0)

    def test_png_transparent(self):
        path = qr.generate_qr_code(URL, self.tmp / "t.png", background="transparent")
        with Image.open(path) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_svg_file(self):
        path = qr.generate_qr_code(URL, self.tmp / "code.svg", fmt="svg", size=800)
        root = ET.parse(path).getroot()
        self.assertEqual(root.get("height"), "800")

    def test_pdf_page_size_follows_size_and_dpi(self):
        path = qr.generate_qr_code(
            URL, self.tmp / "code.pdf", fmt="pdf", size=600, dpi=300
        )
        box = PdfReader(path).pages[0].mediabox
        # 600 px at 300 dpi = 2 inches = 144 points.
        self.assertAlmostEqual(float(box.width), 144.0, places=3)
        self.assertAlmostEqual(float(box.height), 144.0, places=3)

    def test_creates_missing_folders(self):
        path = qr.generate_qr_code("hi", self.tmp / "a" / "b" / "code.png")
        self.assertTrue(path.is_file())

    def test_overwrites_existing_file(self):
        target = self.tmp / "code.png"
        target.write_bytes(b"old")
        qr.generate_qr_code("hi", target)
        with Image.open(target) as image:
            self.assertEqual(image.format, "PNG")

    def test_invalid_options_write_nothing(self):
        target = self.tmp / "code.jpg"
        with self.assertRaises(ValueError):
            qr.generate_qr_code("hi", target, fmt="jpg", background="transparent")
        with self.assertRaises(ValueError):
            qr.generate_qr_code("", self.tmp / "empty.png")
        self.assertEqual(list(self.tmp.iterdir()), [])


@unittest.skipIf(
    cv2 is None, "opencv not installed (uv run --with opencv-python-headless ...)"
)
class TestDecodeRoundTrip(TempDirTestCase):
    """Generated files must decode back to the original text."""

    def test_every_format_decodes(self):
        for fmt in qr.FORMATS:
            with self.subTest(fmt=fmt):
                path = qr.generate_qr_code(
                    URL, self.tmp / f"code.{fmt}", fmt=fmt, size=400
                )
                if fmt in ("svg", "pdf"):
                    image = rasterize_document(path)
                else:
                    image = Image.open(path)
                self.assertEqual(decode_image(image), URL)

    def test_colors_transparency_and_levels_decode(self):
        cases = [
            ("navy", "#fff4d6", "M"),
            ("#1d3557", "transparent", "H"),
            ("darkgreen", "white", "L"),
            ("black", "white", "Q"),
        ]
        for fg, bg, level in cases:
            with self.subTest(fg=fg, bg=bg, level=level):
                path = qr.generate_qr_code(
                    URL,
                    self.tmp / "c.png",
                    foreground=fg,
                    background=bg,
                    error_correction=level,
                    size=400,
                )
                self.assertEqual(decode_image(Image.open(path)), URL)

    def test_unicode_text_decodes(self):
        text = "Grüße aus Jülich – ½ € ✓"
        path = qr.generate_qr_code(text, self.tmp / "u.png", size=400)
        self.assertEqual(decode_image(Image.open(path)), text)


class TestCommandLine(TempDirTestCase):
    """Headless command-line interface."""

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        """Run ``main()`` and capture (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = qr.main(list(argv))
            except SystemExit as exc:  # argparse errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_headless_default_png(self):
        code, out, _ = self.run_main("--headless", URL, "-o", str(self.tmp / "site"))
        self.assertEqual(code, 0)
        self.assertIn("Saved QR code", out)
        with Image.open(self.tmp / "site.png") as image:
            self.assertEqual(image.size, (qr.DEFAULT_SIZE, qr.DEFAULT_SIZE))

    def test_headless_all_options(self):
        code, _, err = self.run_main(
            "--headless",
            URL,
            "-o",
            str(self.tmp / "c"),
            "-f",
            "webp",
            "-s",
            "300",
            "--fg",
            "#1d3557",
            "--bg",
            "transparent",
            "-e",
            "h",
            "--border",
            "2",
            "--dpi",
            "150",
        )
        self.assertEqual((code, err), (0, ""))
        with Image.open(self.tmp / "c.webp") as image:
            self.assertEqual(image.size, (300, 300))
            self.assertEqual(image.mode, "RGBA")

    def test_format_inferred_from_output(self):
        code, _, _ = self.run_main("--headless", URL, "-o", str(self.tmp / "c.svg"))
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "c.svg").is_file())

    def test_warning_printed_for_inverted_colors(self):
        code, _, err = self.run_main(
            "--headless",
            URL,
            "-o",
            str(self.tmp / "c.png"),
            "--fg",
            "white",
            "--bg",
            "black",
        )
        self.assertEqual(code, 0)
        self.assertIn("Warning:", err)

    def test_errors_exit_with_1(self):
        cases = [
            ("-o", str(self.tmp / "c.jpg"), "--bg", "transparent"),
            ("-o", str(self.tmp / "c.png"), "-f", "svg"),
            ("-o", str(self.tmp / "c.png"), "--fg", "blurple"),
            ("-o", str(self.tmp / "c.png"), "-s", "10"),
            ("-o", str(self.tmp / "c"), "-f", "gif"),
        ]
        for extra in cases:
            with self.subTest(args=extra):
                code, _, err = self.run_main("--headless", URL, *extra)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("Error:"))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_headless_requires_data(self):
        for argv in (("--headless",), ("--headless", "   ")):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn("requires the link", err)

    def test_invalid_error_correction_choice(self):
        code, _, _ = self.run_main("--headless", URL, "-e", "Z")
        self.assertEqual(code, 2)


def _make_tk_root():
    """Create a hidden CustomTkinter root, or return None if no GUI is available."""
    if qr.ctk is None:
        return None
    try:
        root = qr.ctk.CTk()
    except qr.tk.TclError:
        return None
    root.withdraw()
    return root


class TestGui(TempDirTestCase):
    """Drive the GUI programmatically (no dialogs are opened)."""

    def setUp(self):
        super().setUp()
        qr.ctk.set_appearance_mode("light")
        self.root = _make_tk_root()
        if self.root is None:
            self.skipTest("no display / tkinter available")
        self.app = qr.QRCodeApp(self.root, output_dir=self.tmp)
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        super().tearDown()

    def items(self, tag: str) -> tuple:
        """Return the preview-canvas items carrying ``tag``."""
        return self.app.preview_canvas.find_withtag(tag)

    def test_empty_form_shows_placeholder_and_disables_save(self):
        self.assertTrue(self.items("placeholder"))
        self.assertFalse(self.items("module"))
        self.assertEqual(self.app.get_data(), "")  # placeholder text is not data
        self.assertEqual(self.app.save_button.cget("state"), "disabled")
        self.assertEqual(
            self.app.target_var.get(), qr.shorten_home(self.tmp / "qrcode.png")
        )

    def test_preview_appears_for_link(self):
        self.app.set_data(URL)
        self.app.refresh_preview()
        self.assertFalse(self.items("placeholder"))
        modules = self.items("module")
        self.assertTrue(modules)
        # Every dark run of the matrix is drawn as one rectangle.
        matrix = qr.build_matrix(URL)
        self.assertEqual(len(modules), len(list(qr.dark_runs(matrix))))
        self.assertEqual(
            self.app.preview_canvas.itemcget(modules[0], "fill"), "#000000"
        )
        self.assertIn("Version", self.app.info_label.cget("text"))
        self.assertEqual(self.app.save_button.cget("state"), "normal")

    def test_drawn_preview_stays_inside_canvas(self):
        self.app.set_data("x" * 1000)  # a large QR version
        self.app.ec_var.set("H")
        self.app.refresh_preview()
        x0, y0, x1, y1 = self.app.preview_canvas.bbox("module")
        side = self.app.PREVIEW_SIZE
        self.assertTrue(0 <= x0 and 0 <= y0 and x1 <= side + 1 and y1 <= side + 1)

    def test_transparent_preview_shows_checkerboard(self):
        self.app.set_data(URL)
        self.app.transparent_var.set(True)
        self.app.refresh_preview()
        self.assertTrue(self.items("checker"))
        self.assertFalse(self.items("background"))
        self.app.transparent_var.set(False)
        self.app.refresh_preview()
        self.assertFalse(self.items("checker"))

    def test_invalid_field_reported_in_status(self):
        self.app.set_data(URL)
        self.app.size_var.set("big")
        self.app.refresh_preview()
        self.assertIn("whole number", self.app.status_var.get())
        self.assertEqual(self.app.save_button.cget("state"), "disabled")
        self.app.size_var.set("1024")
        self.app.fg_var.set("blurple")
        self.app.refresh_preview()
        self.assertIn("Invalid colour", self.app.status_var.get())
        self.assertEqual(self.app.fg_swatch.cget("text"), "?")

    def test_size_too_small_for_raster_only(self):
        self.app.set_data(URL)
        self.app.size_var.set("20")
        self.app.refresh_preview()
        self.assertIn("too small", self.app.status_var.get())
        self.app.format_var.set("SVG")  # vector output has no pixel minimum
        self.app.refresh_preview()
        self.assertEqual(self.app.status_var.get(), "")
        self.assertIn("vector", self.app.info_label.cget("text"))

    def test_warning_for_inverted_colors(self):
        self.app.set_data(URL)
        self.app.fg_var.set("white")
        self.app.bg_var.set("black")
        self.app.refresh_preview()
        self.assertIn("inverted", self.app.status_var.get())
        self.assertEqual(self.app.save_button.cget("state"), "normal")

    def test_size_presets_follow_size_field(self):
        self.app.size_var.set("2048")
        self.assertEqual(self.app.size_presets.get(), "2048")
        self.app.size_var.set("777")
        self.assertEqual(self.app.size_presets.get(), "")
        # Clicking a preset writes into the size field.
        self.app.size_presets.cget("command")("512")
        self.assertEqual(self.app.size_var.get(), "512")

    def test_error_correction_hint(self):
        self.app.ec_var.set("H")
        self.app.refresh_preview()
        self.assertEqual(self.app.collect_options()["error_correction"], "H")
        self.assertIn("30%", self.app.ec_hint.cget("text"))

    def test_save_with_custom_options(self):
        self.app.set_data(URL)
        self.app.size_var.set("512")
        self.app.fg_var.set("#1d3557")
        self.app.transparent_var.set(True)
        self.app.name_var.set("my_link")
        self.app.format_var.set("PNG")
        path = self.app.save()
        self.assertEqual(path, self.tmp / "my_link.png")
        with Image.open(path) as image:
            self.assertEqual((image.size, image.mode), ((512, 512), "RGBA"))

    def test_format_selector_controls_extension(self):
        self.app.name_var.set("poster.png")  # typed extension is replaced
        self.app.format_var.set("SVG")
        self.assertEqual(self.app.target_path(), self.tmp / "poster.svg")
        self.app.refresh_preview()
        self.assertEqual(self.app.extension_label.cget("text"), ".svg")
        self.app.name_var.set("  ")
        with self.assertRaises(ValueError):
            self.app.target_path()

    def test_opaque_format_disables_transparency(self):
        self.app.transparent_var.set(True)
        self.assertEqual(self.app.bg_entry.cget("state"), "disabled")
        self.app.format_var.set("JPG")
        self.assertFalse(self.app.transparent_var.get())
        self.assertEqual(self.app.transparent_switch.cget("state"), "disabled")
        self.assertEqual(self.app.bg_entry.cget("state"), "normal")
        self.app.format_var.set("PDF")
        self.assertEqual(self.app.transparent_switch.cget("state"), "normal")

    def test_clear_and_set_data(self):
        self.app.set_data("  hello  ")
        self.assertEqual(self.app.get_data(), "  hello  ")
        self.app.set_data("")
        self.assertEqual(self.app.get_data(), "")

    def test_link_placeholder_follows_field_content(self):
        placeholder = self.app.link_placeholder
        self.assertEqual(placeholder.winfo_manager(), "place")  # empty on start
        self.app.set_data(URL)
        self.assertEqual(placeholder.winfo_manager(), "")
        self.app.set_data("")
        self.assertEqual(placeholder.winfo_manager(), "place")
        prefilled = qr.QRCodeApp(self.root, data=URL, output_dir=self.tmp)
        self.assertEqual(prefilled.link_placeholder.winfo_manager(), "")

    def test_window_size_stable_across_messages(self):
        def requested_size():
            self.root.update_idletasks()
            return self.root.winfo_reqwidth(), self.root.winfo_reqheight()

        empty = requested_size()
        self.app.set_data(URL)
        self.app.fg_var.set("#eeeeee")  # low contrast + inverted: two warnings
        self.app.bg_var.set("#cccccc")
        self.app.refresh_preview()
        self.assertEqual(
            len(
                qr.scannability_warnings(
                    qr.parse_color("#eeeeee"), qr.parse_color("#cccccc")
                )
            ),
            2,
        )
        self.assertEqual(requested_size(), empty)
        self.app._set_status("✓ Saved qrcode.png", kind="success")
        self.app.reveal_button.grid()
        self.assertEqual(requested_size(), empty)

    def test_appearance_switch_redraws_preview(self):
        try:
            self.app._set_appearance("Dark")
            self.app.refresh_preview()
            dark_bg = self.app.preview_canvas.cget("background")
            self.assertEqual(dark_bg, qr.PALETTE["tile"][1])
        finally:
            qr.ctk.set_appearance_mode("light")
        self.app.refresh_preview()
        self.assertEqual(
            self.app.preview_canvas.cget("background"), qr.PALETTE["tile"][0]
        )

    def test_prefill_from_arguments(self):
        app = qr.QRCodeApp(
            self.root,
            data="hi",
            output_dir=self.tmp,
            name="pre",
            fmt="jpeg",
            background="transparent",
            error_correction="q",
        )
        self.assertEqual(app.get_data(), "hi")
        self.assertEqual(app.format_var.get(), "JPG")
        # JPG cannot be transparent, so the pre-filled transparency is dropped.
        self.assertFalse(app.transparent_var.get())
        self.assertEqual(app.collect_options()["error_correction"], "Q")
        self.assertEqual(app.save(), self.tmp / "pre.jpg")

    def test_unknown_prefill_values_rejected(self):
        with self.assertRaises(ValueError):
            qr.QRCodeApp(self.root, fmt="gif")
        with self.assertRaises(ValueError):
            qr.QRCodeApp(self.root, error_correction="Z")


class TestGuiHelpers(unittest.TestCase):
    """Pure helper functions used by the GUI."""

    def test_blend_hex(self):
        self.assertEqual(qr._blend_hex((0, 0, 0, 255), WHITE), "#000000")
        self.assertEqual(qr._blend_hex((0, 0, 0, 0), WHITE), "#ffffff")
        self.assertEqual(qr._blend_hex((0, 0, 0, 128), WHITE), "#7f7f7f")

    def test_shorten_home(self):
        self.assertEqual(qr.shorten_home(Path.home() / "a" / "b.png"), "~/a/b.png")
        self.assertEqual(qr.shorten_home(Path("/opt/x.png")), "/opt/x.png")

    def test_to_int(self):
        self.assertEqual(qr._to_int(" 42 ", "Size"), 42)
        with self.assertRaises(ValueError):
            qr._to_int("4.2", "Size")


if __name__ == "__main__":
    unittest.main()
