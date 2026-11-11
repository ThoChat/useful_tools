"""Unit tests for ``watermark.py``.

Run from the repository root:
  uv run python -m unittest discover -s tests -v

GUI tests are skipped when no display (or no tkinter) is available.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageCms

# The tool is a standalone script in the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watermark as wmk  # noqa: E402

GREY = (128, 128, 128)


def make_photo(path: Path, size=(600, 400), mode="RGB", **save_options) -> Path:
    """Write a test image with a colour gradient (or a transparent one)."""
    width, height = size
    gradient = Image.linear_gradient("L").resize(size)
    image = Image.merge(
        "RGB",
        (
            gradient,
            gradient.transpose(Image.Transpose.ROTATE_90).resize(size),
            Image.new("L", size, 90),
        ),
    )
    if mode == "RGBA":
        image = image.convert("RGBA")
        alpha = Image.new("L", size, 255)
        alpha.paste(0, (0, 0, width // 2, height))  # left half transparent
        image.putalpha(alpha)
    image.save(path, **save_options)
    return path


def make_watermark(**overrides) -> wmk.Watermark:
    """Build a watermark that is easy to see in pixel tests."""
    options = {"color": "white", "opacity": 100, "font_size": "8%"}
    options.update(overrides)
    return wmk.make_watermark(**options)


def changed_box(before: Image.Image, after: Image.Image):
    """Return the bounding box of pixels that differ between two images."""
    return ImageChops.difference(before.convert("RGB"), after.convert("RGB")).getbbox()


class TempDirTestCase(unittest.TestCase):
    """Base class providing a fresh temporary directory per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestParsing(unittest.TestCase):
    """Option parsing helpers."""

    def test_font_size_pixels(self):
        expected = wmk.FontSize(60, False)
        for spec in ("60", 60, " 60 px ", "60.4"):
            with self.subTest(spec=spec):
                self.assertEqual(wmk.parse_font_size(spec), expected)
        self.assertEqual(str(expected), "60")

    def test_font_size_percent(self):
        self.assertEqual(wmk.parse_font_size("5%"), wmk.FontSize(5.0, True))
        self.assertEqual(wmk.parse_font_size(" 7.5 % "), wmk.FontSize(7.5, True))
        self.assertEqual(str(wmk.FontSize(5.0, True)), "5%")

    def test_font_size_invalid(self):
        for spec in (
            "abc",
            "",
            "0",
            "-3",
            "0%",
            "101%",
            "nan%",
            str(wmk.MAX_FONT_PX + 1),
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                wmk.parse_font_size(spec)

    def test_font_size_px(self):
        self.assertEqual(wmk.font_size_px(wmk.FontSize(5, True), (1200, 800)), 40)
        self.assertEqual(wmk.font_size_px(wmk.FontSize(5, True), (800, 1200)), 40)
        self.assertEqual(wmk.font_size_px(wmk.FontSize(60, False), (10, 10)), 60)
        self.assertEqual(wmk.font_size_px(wmk.FontSize(0.1, True), (50, 50)), 1)

    def test_formats_and_layouts(self):
        self.assertEqual(wmk.normalize_format(".JPEG"), "jpg")
        self.assertEqual(wmk.normalize_format("tif"), "tiff")
        with self.assertRaises(ValueError):
            wmk.normalize_format("gif")
        self.assertEqual(wmk.format_from_suffix(Path("a.WebP")), "webp")
        self.assertIsNone(wmk.format_from_suffix(Path("a.gif")))
        self.assertEqual(wmk.normalize_layout(" Center "), "centre")
        self.assertEqual(wmk.normalize_layout("TILED"), "tiled")
        with self.assertRaises(ValueError):
            wmk.normalize_layout("diagonal")

    def test_colors(self):
        self.assertEqual(wmk.parse_color("white"), (255, 255, 255, 255))
        self.assertEqual(wmk.parse_color(" #c8c8c8 "), (200, 200, 200, 255))
        self.assertEqual(wmk.parse_color("#ff000080"), (255, 0, 0, 128))
        for value in ("blurple", "", "#12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wmk.parse_color(value)
        self.assertEqual(wmk.color_to_hex((200, 200, 200, 10)), "#c8c8c8")

    def test_outline_contrasts_with_text(self):
        self.assertEqual(wmk.outline_color(wmk.parse_color("white")), (0, 0, 0, 255))
        self.assertEqual(wmk.outline_color(wmk.parse_color("#c8c8c8")), (0, 0, 0, 255))
        self.assertEqual(
            wmk.outline_color(wmk.parse_color("navy")), (255, 255, 255, 255)
        )

    def test_clean_path_text(self):
        self.assertEqual(wmk.clean_path_text("  "), "")
        self.assertEqual(
            wmk.clean_path_text('"/tmp/my photo.jpg"\n'), "/tmp/my photo.jpg"
        )
        self.assertEqual(wmk.clean_path_text("'/tmp/a.png'"), "/tmp/a.png")
        self.assertEqual(
            wmk.clean_path_text("file:///tmp/my%20photo.jpg"), "/tmp/my photo.jpg"
        )
        self.assertEqual(
            wmk.clean_path_text("/no/such/my\\ photo.jpg"), "/no/such/my photo.jpg"
        )


class TestFonts(TempDirTestCase):
    """Font discovery and validation."""

    def test_families_load(self):
        for family in wmk.FONT_FAMILIES:
            with self.subTest(family=family):
                path = wmk.resolve_font_file(family)
                self.assertTrue(path is None or Path(path).is_file())
                self.assertGreater(wmk.load_font(family, 40).size, 0)
                self.assertTrue(wmk.font_display_name(family))

    def test_custom_font_file(self):
        path = wmk.resolve_font_file("sans")
        if path is None:
            self.skipTest("no system sans font installed")
        self.assertEqual(wmk.normalize_font(path), path)
        self.assertEqual(wmk.normalize_font(" SANS "), "sans")

    def test_bad_font_files(self):
        not_a_font = self.tmp / "font.ttf"
        not_a_font.write_text("hello")
        for font in (str(self.tmp / "missing.ttf"), str(not_a_font), "  "):
            with self.subTest(font=font), self.assertRaises(ValueError):
                wmk.normalize_font(font)


class TestMakeWatermark(unittest.TestCase):
    """Validation of the watermark settings."""

    def test_defaults(self):
        wm = wmk.make_watermark()
        self.assertEqual(wm.text, "All rights reserved")
        self.assertEqual(wm.font_size, wmk.FontSize(5, True))
        self.assertEqual((wm.opacity, wm.angle, wm.layout), (30, 30.0, "tiled"))
        self.assertEqual(wm.color, (200, 200, 200, 255))

    def test_values_are_normalised(self):
        wm = wmk.make_watermark(
            text="  © Me  ", font_size="40px", font="Serif", layout="center"
        )
        self.assertEqual(
            (wm.text, wm.font_size, wm.font, wm.layout),
            ("© Me", wmk.FontSize(40, False), "serif", "centre"),
        )

    def test_invalid_values(self):
        cases = [
            {"text": "   "},
            {"color": "#ffffff00"},
            {"color": "nope"},
            {"opacity": -1},
            {"opacity": 101},
            {"angle": 91},
            {"angle": -90.5},
            {"spacing": -0.1},
            {"spacing": wmk.MAX_SPACING + 1},
            {"layout": "spiral"},
            {"font_size": "0"},
            {"font": "/no/such/font.ttf"},
        ]
        for options in cases:
            with self.subTest(**options), self.assertRaises(ValueError):
                wmk.make_watermark(**options)


class TestRendering(unittest.TestCase):
    """Watermark layers and compositing."""

    def setUp(self):
        self.black = Image.new("RGB", (600, 400), "black")

    def test_tiled_pattern_reaches_every_corner(self):
        # The old version rotated an image-sized overlay, leaving the
        # corners without any text.
        for angle in (0, 30, 45, -60, 90):
            with self.subTest(angle=angle):
                result = wmk.apply_watermark(self.black, make_watermark(angle=angle))
                for box in (
                    (0, 0, 150, 100),
                    (450, 0, 600, 100),
                    (0, 300, 150, 400),
                    (450, 300, 600, 400),
                ):
                    self.assertIsNotNone(result.crop(box).getbbox(), f"corner {box}")

    def test_one_copy_sits_in_the_middle(self):
        result = wmk.apply_watermark(self.black, make_watermark(text="I", angle=0))
        self.assertEqual(result.getpixel((300, 200)), (255, 255, 255))

    def test_overlapping_copies_do_not_get_darker(self):
        wm = make_watermark(opacity=50, spacing=0)
        result = wmk.apply_watermark(self.black, wm)
        # 50 % white over black is at most 128, however many copies overlap.
        self.assertLessEqual(max(band[1] for band in result.getextrema()), 128)

    def test_opacity_zero_changes_nothing(self):
        photo = make_photo(Path(tempfile.mkdtemp()) / "p.png")
        image = Image.open(photo).convert("RGB")
        result = wmk.apply_watermark(image, make_watermark(opacity=0))
        self.assertIsNone(changed_box(image, result))

    def test_colour_alpha_multiplies_opacity(self):
        half = wmk.apply_watermark(self.black, make_watermark(opacity=50))
        alpha = wmk.apply_watermark(self.black, make_watermark(color="#ffffff80"))
        diff = ImageChops.difference(half, alpha).getextrema()
        self.assertLessEqual(max(band[1] for band in diff), 2)

    def test_centre_layout(self):
        wm = make_watermark(layout="centre", angle=0)
        layer = wmk.build_layer(self.black.size, wm)
        self.assertEqual((layer.count, layer.clipped), (1, False))
        left, top, right, bottom = changed_box(
            self.black, wmk.apply_watermark(self.black, wm)
        )
        self.assertAlmostEqual((left + right) / 2, 300, delta=3)
        self.assertAlmostEqual((top + bottom) / 2, 200, delta=3)
        self.assertGreater(right - left, bottom - top)  # horizontal text

    def test_vertical_text_at_90_degrees(self):
        wm = make_watermark(layout="centre", angle=90)
        left, top, right, bottom = changed_box(
            self.black, wmk.apply_watermark(self.black, wm)
        )
        self.assertGreater(bottom - top, right - left)

    def test_corner_layout(self):
        wm = make_watermark(layout="corner", angle=0, font_size="5%")
        left, top, right, bottom = changed_box(
            self.black, wmk.apply_watermark(self.black, wm)
        )
        margin = round(400 * wmk.CORNER_MARGIN)
        self.assertGreater(left, 300)
        self.assertGreater(top, 200)
        self.assertAlmostEqual(right, 600 - margin, delta=2)
        self.assertAlmostEqual(bottom, 400 - margin, delta=2)

    def test_text_too_big_is_clipped(self):
        wm = make_watermark(layout="centre", font_size="60%")
        self.assertTrue(wmk.build_layer(self.black.size, wm).clipped)

    def test_spacing_controls_density(self):
        dense = wmk.build_layer(self.black.size, make_watermark(spacing=0.5))
        sparse = wmk.build_layer(self.black.size, make_watermark(spacing=4))
        self.assertGreater(dense.count, sparse.count)

    def test_tiny_text_renders_in_every_font(self):
        # macOS's Helvetica cannot be drawn below 8 px; tiny text is common
        # in the preview of a large photo with a pixel text size.
        for family in wmk.FONT_FAMILIES:
            with self.subTest(family=family):
                wm = make_watermark(font=family, font_size="10")
                layer = wmk.build_layer((300, 200), wm, scale=0.1)
                self.assertEqual(layer.font_px, 1)
                stamp = wmk.render_stamp(make_watermark(font=family, angle=0), 4)
                self.assertLessEqual(stamp.text_size[1], 5)

    def test_too_many_copies_rejected(self):
        wm = make_watermark(font_size="1", spacing=0, text=".")
        with self.assertRaises(ValueError):
            wmk.build_layer((5000, 5000), wm)

    def test_preview_scale_matches_full_size(self):
        relative = make_watermark(font_size="5%")
        self.assertEqual(wmk.build_layer((1200, 800), relative).font_px, 40)
        self.assertEqual(wmk.build_layer((300, 200), relative, scale=0.25).font_px, 10)
        absolute = make_watermark(font_size="60")
        self.assertEqual(wmk.build_layer((300, 200), absolute, scale=0.25).font_px, 15)

    def test_outline_adds_contrasting_edge(self):
        grey = Image.new("RGB", (600, 400), GREY)
        plain = wmk.apply_watermark(grey, make_watermark())
        outlined = wmk.apply_watermark(grey, make_watermark(outline=True))
        self.assertEqual(plain.getextrema()[0][0], GREY[0])  # nothing darker
        self.assertLess(outlined.getextrema()[0][0], 40)  # dark outline pixels

    def test_transparency_is_preserved(self):
        clear = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
        result = wmk.apply_watermark(clear, make_watermark(layout="centre"))
        self.assertEqual(result.mode, "RGBA")
        self.assertEqual(result.getpixel((0, 0))[3], 0)
        self.assertGreater(result.getextrema()[3][1], 0)

    def test_other_modes_are_converted(self):
        grey = Image.new("L", (200, 100), 0)
        self.assertEqual(wmk.apply_watermark(grey, make_watermark()).mode, "RGB")
        palette = Image.new("P", (200, 100), 0)
        palette.info["transparency"] = 0
        self.assertEqual(wmk.apply_watermark(palette, make_watermark()).mode, "RGBA")


class TestWarnings(unittest.TestCase):
    """Warnings about settings that probably won't look as intended."""

    def test_no_warnings_for_defaults(self):
        self.assertEqual(
            wmk.watermark_warnings((600, 400), wmk.make_watermark(), "jpg"), []
        )

    def test_low_and_zero_opacity(self):
        low = wmk.watermark_warnings((600, 400), wmk.make_watermark(opacity=5))
        zero = wmk.watermark_warnings((600, 400), wmk.make_watermark(opacity=0))
        self.assertIn("hard to see", low[0])
        self.assertIn("invisible", zero[0])

    def test_text_cut_off(self):
        for layout in ("centre", "corner"):
            with self.subTest(layout=layout):
                wm = wmk.make_watermark(layout=layout, font_size="40%")
                self.assertIn(
                    "cut off", " ".join(wmk.watermark_warnings((600, 400), wm))
                )
        tiled = wmk.make_watermark(font_size="40%")
        self.assertEqual(wmk.watermark_warnings((600, 400), tiled), [])

    def test_transparency_lost_in_jpg(self):
        wm = wmk.make_watermark()
        jpg = wmk.watermark_warnings((600, 400), wm, "jpg", has_transparency=True)
        self.assertIn("transparency", jpg[0])
        self.assertEqual(wmk.watermark_warnings((600, 400), wm, "png", True), [])


class TestFiles(TempDirTestCase):
    """Opening, saving and output paths."""

    def test_open_image_basics(self):
        source = wmk.open_image(make_photo(self.tmp / "p.jpg"))
        self.assertEqual((source.image.size, source.image.mode), ((600, 400), "RGB"))
        self.assertEqual(source.format, "jpg")
        self.assertFalse(source.has_transparency)

    def test_open_image_applies_exif_orientation(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # stored sideways: "rotate 90° clockwise to view"
        path = self.tmp / "phone.jpg"
        Image.new("RGB", (200, 100), "red").save(path, exif=exif)
        self.assertEqual(wmk.open_image(path).image.size, (100, 200))

    def test_open_image_transparency(self):
        source = wmk.open_image(make_photo(self.tmp / "t.png", mode="RGBA"))
        self.assertTrue(source.has_transparency)
        # An alpha channel without transparent pixels is dropped.
        opaque = self.tmp / "o.png"
        Image.new("RGBA", (50, 50), (1, 2, 3, 255)).save(opaque)
        self.assertEqual(wmk.open_image(opaque).image.mode, "RGB")

    def test_open_16_bit_greyscale(self):
        path = self.tmp / "scan.png"
        Image.new("I;16", (40, 30), 32768).save(path)
        source = wmk.open_image(path)
        self.assertAlmostEqual(source.image.getpixel((0, 0))[0], 128, delta=1)

    def test_unwritable_source_format(self):
        path = self.tmp / "anim.gif"
        Image.new("P", (40, 30)).save(path)
        self.assertIsNone(wmk.open_image(path).format)

    def test_open_errors(self):
        not_image = self.tmp / "notes.jpg"
        not_image.write_text("not an image")
        for path in (self.tmp / "missing.png", not_image):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                wmk.open_image(path)

    def test_export_every_format(self):
        source = wmk.open_image(make_photo(self.tmp / "p.png"))
        for fmt, spec in wmk.FORMATS.items():
            with self.subTest(fmt=fmt):
                path = wmk.export_watermarked(
                    source, self.tmp / f"out.{fmt}", wmk.make_watermark(), fmt
                )
                with Image.open(path) as image:
                    self.assertEqual(image.format, spec.pil_name)
                    self.assertEqual(image.size, (600, 400))

    def test_export_keeps_colour_profile_and_dpi(self):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        photo = make_photo(self.tmp / "p.jpg", icc_profile=icc, dpi=(300, 300))
        source = wmk.open_image(photo)
        for fmt in ("jpg", "png", "tiff"):
            with self.subTest(fmt=fmt):
                path = wmk.export_watermarked(
                    source, self.tmp / f"out.{fmt}", wmk.make_watermark(), fmt
                )
                with Image.open(path) as image:
                    self.assertEqual(image.info.get("icc_profile"), icc)
                    self.assertAlmostEqual(float(image.info["dpi"][0]), 300, places=0)

    def test_quality_changes_file_size(self):
        source = wmk.open_image(make_photo(self.tmp / "p.png"))
        wm = wmk.make_watermark()
        small = wmk.export_watermarked(source, self.tmp / "q20.jpg", wm, "jpg", 20)
        large = wmk.export_watermarked(source, self.tmp / "q95.jpg", wm, "jpg", 95)
        self.assertLess(small.stat().st_size, large.stat().st_size)
        with self.assertRaises(ValueError):
            wmk.export_watermarked(source, self.tmp / "q0.jpg", wm, "jpg", 0)

    def test_transparency_kept_or_flattened(self):
        source = wmk.open_image(make_photo(self.tmp / "t.png", mode="RGBA"))
        wm = wmk.make_watermark(opacity=0)
        png = wmk.export_watermarked(source, self.tmp / "out.png", wm, "png")
        jpg = wmk.export_watermarked(source, self.tmp / "out.jpg", wm, "jpg")
        with Image.open(png) as image:
            self.assertEqual((image.mode, image.getpixel((0, 0))[3]), ("RGBA", 0))
        with Image.open(jpg) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertGreater(min(image.getpixel((5, 5))), 245)  # white

    def test_opaque_source_stays_rgb(self):
        source = wmk.open_image(make_photo(self.tmp / "p.jpg"))
        path = wmk.export_watermarked(
            source, self.tmp / "out.png", wmk.make_watermark(), "png"
        )
        with Image.open(path) as image:
            self.assertEqual(image.mode, "RGB")

    def test_export_creates_folders(self):
        source = wmk.open_image(make_photo(self.tmp / "p.png"))
        path = wmk.export_watermarked(
            source, self.tmp / "a" / "b" / "out.png", wmk.make_watermark(), "png"
        )
        self.assertTrue(path.is_file())

    def test_original_is_never_overwritten(self):
        photo = make_photo(self.tmp / "p.png")
        before = photo.read_bytes()
        source = wmk.open_image(photo)
        for target in (photo, self.tmp / "." / "p.png"):
            with self.subTest(target=str(target)), self.assertRaises(ValueError):
                wmk.export_watermarked(source, target, wmk.make_watermark(), "png")
        self.assertEqual(photo.read_bytes(), before)

    def test_resolve_output_path(self):
        photo = self.tmp / "holiday.jpg"
        cases = [
            ((None, None, "jpg"), (self.tmp / "holiday_watermarked.jpg", "jpg")),
            ((None, "png", "jpg"), (self.tmp / "holiday_watermarked.png", "png")),
            ((None, None, None), (self.tmp / "holiday_watermarked.png", "png")),
            (("out.webp", None, "jpg"), (Path("out.webp"), "webp")),
            (("out", None, "jpg"), (Path("out.jpg"), "jpg")),
            (("v1.2", "tif", None), (Path("v1.2.tiff"), "tiff")),
            (
                (str(self.tmp), None, "jpg"),
                (self.tmp / "holiday_watermarked.jpg", "jpg"),
            ),
        ]
        for (output, fmt, source_format), expected in cases:
            with self.subTest(output=output, fmt=fmt):
                self.assertEqual(
                    wmk.resolve_output_path(output, photo, fmt, source_format), expected
                )
        with self.assertRaises(ValueError):
            wmk.resolve_output_path("out.png", photo, "jpg")


class TestCommandLine(TempDirTestCase):
    """Headless command-line interface."""

    def setUp(self):
        super().setUp()
        self.photo = make_photo(self.tmp / "photo.jpg")

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        """Run ``main()`` and capture (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = wmk.main(list(argv))
            except SystemExit as exc:  # argparse errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_default_output_next_to_input(self):
        code, out, _ = self.run_main("--headless", str(self.photo))
        self.assertEqual(code, 0)
        target = self.tmp / "photo_watermarked.jpg"
        self.assertIn(str(target), out)
        with Image.open(target) as image, Image.open(self.photo) as original:
            self.assertEqual(image.size, original.size)
            self.assertIsNotNone(changed_box(original, image))

    def test_original_positional_usage_still_works(self):
        out_path = self.tmp / "out.png"
        code, _, err = self.run_main(
            "--headless",
            str(self.photo),
            str(out_path),
            "--text",
            "Your Text",
            "--opacity",
            "30",
            "--font-size",
            "60",
        )
        self.assertEqual((code, err), (0, ""))
        with Image.open(out_path) as image:
            self.assertEqual(image.format, "PNG")

    def test_all_options(self):
        code, _, err = self.run_main(
            "--headless",
            str(self.photo),
            str(self.tmp / "signed"),
            "-t",
            "© Jane",
            "--font",
            "serif",
            "--font-size",
            "4%",
            "--colour",
            "white",
            "--opacity",
            "70",
            "--outline",
            "--layout",
            "center",
            "--angle",
            "-15",
            "--spacing",
            "1",
            "-f",
            "webp",
            "-q",
            "80",
        )
        self.assertEqual((code, err), (0, ""))
        with Image.open(self.tmp / "signed.webp") as image:
            self.assertEqual(image.format, "WEBP")

    def test_warning_printed(self):
        code, _, err = self.run_main("--headless", str(self.photo), "--opacity", "0")
        self.assertEqual(code, 0)
        self.assertIn("Warning:", err)

    def test_errors_exit_with_1(self):
        cases = [
            (str(self.tmp / "missing.jpg"),),
            (str(self.photo), "--color", "blurple"),
            (str(self.photo), "--font-size", "huge"),
            (str(self.photo), "--angle", "120"),
            (str(self.photo), "-q", "0"),
            (str(self.photo), str(self.tmp / "x.png"), "-f", "jpg"),
            (str(self.photo), str(self.photo)),  # would replace the original
            (str(self.photo), "--font", str(self.tmp / "nope.ttf")),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, err = self.run_main("--headless", *args)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("Error:"), err)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["photo.jpg"])

    def test_usage_errors_exit_with_2(self):
        for argv in (
            ("--headless",),
            ("--headless", str(self.photo), "--layout", "spiral"),
        ):
            with self.subTest(argv=argv):
                code, _, _ = self.run_main(*argv)
                self.assertEqual(code, 2)


def _make_tk_root():
    """Create a hidden CustomTkinter root, or return None if no GUI is available."""
    if wmk.ctk is None:
        return None
    try:
        root = wmk.ctk.CTk()
    except wmk.tk.TclError:
        return None
    root.withdraw()
    return root


class TestGui(TempDirTestCase):
    """Drive the GUI programmatically (no dialogs are opened)."""

    def setUp(self):
        super().setUp()
        self.root = _make_tk_root()
        if self.root is None:
            self.skipTest("no display / tkinter available")
        wmk.ctk.set_appearance_mode("light")
        self.photo = make_photo(self.tmp / "photo.jpg")
        self.app = wmk.WatermarkApp(self.root)
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        super().tearDown()

    def open_photo(self, path: Path | None = None) -> None:
        """Load an image into the app and render the preview."""
        self.app.set_image_path(path or self.photo)
        self.app.refresh_preview()

    def items(self, tag: str) -> tuple:
        """Return the preview-canvas items carrying ``tag``."""
        return self.app.preview_canvas.find_withtag(tag)

    def shown_photo(self) -> str:
        """Name of the Tk image displayed in the preview."""
        return self.app.preview_canvas.itemcget(self.items("image")[0], "image")

    def assert_can_save(self, expected: bool) -> None:
        state = self.app.save_button.cget("state")
        self.assertEqual(state, "normal" if expected else "disabled")

    def test_empty_form(self):
        self.assertTrue(self.items("placeholder"))
        self.assertFalse(self.items("image"))
        self.assert_can_save(False)
        self.assertEqual(self.app.target_var.get(), "")
        self.assertEqual(self.app.image_placeholder.winfo_manager(), "place")

    def test_opening_image_shows_preview_and_fills_export(self):
        self.open_photo()
        self.assertTrue(self.items("image"))
        self.assertFalse(self.items("placeholder"))
        self.assertEqual(self.app.image_placeholder.winfo_manager(), "")
        self.assertEqual(self.app.name_var.get(), "photo_watermarked")
        self.assertEqual(self.app.format_var.get(), "JPG")
        self.assertEqual(Path(self.app.folder_var.get()), self.tmp.resolve())
        self.assertIn("600 × 400 px", self.app.info_label.cget("text"))
        self.assertEqual(self.app.size_hint.cget("text"), "= 20 px")
        self.assertEqual(self.app.status_var.get(), "")
        self.assert_can_save(True)

    def test_save_writes_watermarked_file(self):
        self.open_photo()
        self.app.text_var.set("© Test")
        self.app.format_var.set("PNG")
        path = self.app.save()
        self.assertEqual(path, self.tmp.resolve() / "photo_watermarked.png")
        with Image.open(path) as image, Image.open(self.photo) as original:
            self.assertEqual((image.format, image.size), ("PNG", (600, 400)))
            self.assertIsNotNone(changed_box(original, image))

    def test_save_without_image_fails(self):
        with self.assertRaises(ValueError):
            self.app.save()

    def test_prefilled_export_fields_kept_for_first_image(self):
        out = self.tmp / "out"
        app = wmk.WatermarkApp(
            self.root,
            image_path=str(self.photo),
            output_dir=out,
            name="custom",
            fmt="tif",
        )
        self.assertEqual(
            (app.name_var.get(), app.format_var.get(), app.folder_var.get()),
            ("custom", "TIFF", str(out)),
        )
        other = make_photo(self.tmp / "second.png")
        app.set_image_path(other)
        app.refresh_preview()
        self.assertEqual(
            (app.name_var.get(), app.format_var.get()), ("second_watermarked", "PNG")
        )

    def test_quoted_path_is_accepted(self):
        self.open_photo(f'"{self.photo}"')
        self.assertTrue(self.items("image"))

    def test_missing_image_reported(self):
        self.open_photo(self.tmp / "missing.jpg")
        self.assertIn("Image not found", self.app.status_var.get())
        self.assertTrue(self.items("placeholder"))
        self.assert_can_save(False)
        self.app.set_image_path("")
        self.app.refresh_preview()
        self.assertEqual(self.app.status_var.get(), "")

    def test_invalid_fields_reported(self):
        self.open_photo()
        self.app.color_var.set("blurple")
        self.app.refresh_preview()
        self.assertIn("Invalid colour", self.app.status_var.get())
        self.assertEqual(self.app.color_swatch.cget("text"), "?")
        self.assert_can_save(False)
        self.assertTrue(self.items("image"))  # the original is still shown

        self.app.color_var.set("white")
        self.app.size_var.set("huge")
        self.app.refresh_preview()
        self.assertIn("Text size", self.app.status_var.get())
        self.assertEqual(self.app.size_hint.cget("text"), "")

        self.app.size_var.set("5%")
        self.app.text_var.set("  ")
        self.app.refresh_preview()
        self.assertIn("watermark text", self.app.status_var.get())

    def test_quality_only_checked_for_lossy_formats(self):
        self.open_photo()
        self.app.quality_var.set("best")
        self.app.refresh_preview()
        self.assertIn("Quality", self.app.status_var.get())
        self.app.format_var.set("PNG")
        self.app.refresh_preview()
        self.assertEqual(self.app.status_var.get(), "")
        self.assertEqual(self.app.quality_entry.cget("state"), "disabled")
        self.app.format_var.set("WEBP")
        self.assertEqual(self.app.quality_entry.cget("state"), "normal")

    def test_size_presets_and_hint(self):
        self.open_photo()
        self.app.size_var.set("8%")
        self.app.refresh_preview()
        self.assertEqual(self.app.size_presets.get(), "8%")
        self.assertEqual(self.app.size_hint.cget("text"), "= 32 px")
        self.app.size_var.set("60")
        self.app.refresh_preview()
        self.assertEqual(self.app.size_presets.get(), "")
        self.assertEqual(self.app.size_hint.cget("text"), "px")
        self.app.size_presets.cget("command")("12%")
        self.assertEqual(self.app.size_var.get(), "12%")

    def test_font_controls(self):
        self.app.font_presets.cget("command")("Mono")
        self.assertEqual(self.app.font_var.get(), "mono")
        self.assertEqual(self.app.font_presets.get(), "Mono")
        path = wmk.resolve_font_file("sans")
        if path is not None:
            self.app.font_var.set(path)
            self.app.refresh_preview()
            self.assertEqual(self.app.font_presets.get(), "")
            self.assertTrue(self.app.font_hint.cget("text"))

    def test_spacing_only_for_tiled_layout(self):
        self.app.layout_selector.cget("command")("Centred")
        self.assertEqual(self.app.layout_var.get(), "centre")
        self.assertEqual(self.app.spacing_slider.cget("state"), "disabled")
        self.app.layout_var.set("tiled")
        self.assertEqual(self.app.layout_selector.get(), "Tiled")
        self.assertEqual(self.app.spacing_slider.cget("state"), "normal")

    def test_slider_labels(self):
        self.app.opacity_var.set(55)
        self.app.angle_var.set(-45)
        self.app.spacing_var.set(2.25)
        self.assertEqual(
            [
                label.cget("text")
                for label in (
                    self.app.opacity_label,
                    self.app.angle_label,
                    self.app.spacing_label,
                )
            ],
            ["55%", "-45°", "2.25×"],
        )

    def test_warning_for_transparency_in_jpg(self):
        self.open_photo(make_photo(self.tmp / "logo.png", mode="RGBA"))
        self.assertTrue(self.items("checker"))
        self.app.format_var.set("JPG")
        self.app.refresh_preview()
        self.assertIn("transparency", self.app.status_var.get())
        self.assert_can_save(True)

    def test_original_cannot_be_the_target(self):
        self.open_photo()
        self.app.name_var.set("photo")
        self.app.refresh_preview()
        self.assertIn("original", self.app.status_var.get())
        self.assert_can_save(False)

    def test_hold_to_compare(self):
        self.open_photo()
        watermarked = str(self.app._photos["watermarked"])
        self.assertEqual(self.shown_photo(), watermarked)
        self.app._on_preview_press()
        self.assertEqual(self.shown_photo(), str(self.app._photos["original"]))
        self.app._on_preview_release()
        self.assertEqual(self.shown_photo(), watermarked)

    def test_no_stale_compare_after_error(self):
        self.open_photo()
        self.app.color_var.set("blurple")
        self.app.refresh_preview()
        shown = self.shown_photo()
        self.app._on_preview_press()
        self.app._on_preview_release()
        self.assertEqual(self.shown_photo(), shown)

    def test_preview_differs_from_original(self):
        self.open_photo()
        base = self.app._preview_base
        layer = wmk.build_layer(
            base.size, self.app.collect_watermark(), self.app._preview_scale
        )
        self.assertGreater(layer.count, 1)
        self.assertIsNotNone(changed_box(base, wmk.composite(base, layer.image)))

    def test_window_size_stable_across_messages(self):
        def requested_size():
            self.root.update_idletasks()
            return self.root.winfo_reqwidth(), self.root.winfo_reqheight()

        self.open_photo()
        loaded = requested_size()
        self.app.opacity_var.set(0)
        self.app.layout_var.set("centre")
        self.app.size_var.set("60%")
        self.app.refresh_preview()
        self.assertGreaterEqual(self.app.status_var.get().count("."), 2)  # 2 warnings
        self.assertEqual(requested_size(), loaded)
        self.app._set_status("✓ Saved photo_watermarked.jpg", kind="success")
        self.app.reveal_button.grid()
        self.assertEqual(requested_size(), loaded)

    def test_appearance_switch_redraws_preview(self):
        try:
            self.app._set_appearance("Dark")
            self.app.refresh_preview()
            dark = self.app.preview_canvas.cget("background")
            self.assertEqual(dark, wmk.PALETTE["tile"][1])
        finally:
            wmk.ctk.set_appearance_mode("light")
        self.app.refresh_preview()
        self.assertEqual(
            self.app.preview_canvas.cget("background"), wmk.PALETTE["tile"][0]
        )

    def test_unknown_prefill_values_rejected(self):
        with self.assertRaises(ValueError):
            wmk.WatermarkApp(self.root, fmt="gif")
        with self.assertRaises(ValueError):
            wmk.WatermarkApp(self.root, layout="spiral")


class TestGuiHelpers(unittest.TestCase):
    """Pure helper functions used by the GUI."""

    def test_fit_size(self):
        self.assertEqual(wmk.fit_size((1200, 800), 300), (300, 200))
        self.assertEqual(wmk.fit_size((800, 1200), 300), (200, 300))
        self.assertEqual(wmk.fit_size((100, 50), 300), (300, 150))  # scaled up
        self.assertEqual(wmk.fit_size((10000, 1), 300), (300, 1))

    def test_shorten_home(self):
        self.assertEqual(wmk.shorten_home(Path.home() / "a" / "b.png"), "~/a/b.png")
        self.assertEqual(wmk.shorten_home(Path("/opt/x.png")), "/opt/x.png")

    def test_to_int(self):
        self.assertEqual(wmk._to_int(" 42 ", "Quality"), 42)
        with self.assertRaises(ValueError):
            wmk._to_int("4.2", "Quality")


if __name__ == "__main__":
    unittest.main()
