# Useful Tools

A collection of Python tools useful for everyday purposes.

## Available Tools

### merge_pdfs.py
Merge PDF and SVG files into a single PDF (SVGs are automatically converted to PDF).

**Usage:**
```bash
uv run merge_pdfs.py output_name.pdf input1.pdf input2.svg ...
```

**Dependencies:** pypdf, svglib, reportlab

---

### pdf_spliter.py
Split a PDF into individual pages, each saved as a separate PDF file.

Creates a folder named after the input PDF (without extension) inside the
output directory, containing one PDF per page of the original document.

**Usage:**
```bash
uv run pdf_spliter.py input.pdf
uv run pdf_spliter.py input.pdf --output-dir /path/to/output --digits 4
```

**Options:**
- `input` - Path to the input PDF file (required)
- `--output-dir` - Directory to create the output folder in (default: current directory)
- `--digits` - Number of zero-padded digits in page filenames (default: 3)

**Dependencies:** pypdf

---

### watermark.py
Add a text watermark to an image. By default it opens a small GUI (same look
as the QR code generator: card layout, segmented controls, light/dark mode)
where you open an image, type your text, see a live preview, and choose the
font, size, colour, outline, opacity, layout (tiled pattern, centred, or a
bottom-right signature), angle, spacing, output format and file name.
Press and hold the preview to compare with the original. Use `--headless`
to skip the window and write the file directly; without `--headless`, the
command-line options pre-fill the GUI.

The text size can be given in pixels (`60`) or as a percentage of the
image's shorter side (`5%`), so the same settings suit small and large
photos. Phone photos are turned upright using their EXIF orientation; the
colour profile and DPI are kept, other metadata (camera, GPS...) is not
copied. Transparent images stay transparent in PNG/TIFF/WEBP. The original
image is never overwritten.

**Usage:**
```bash
uv run watermark.py                                   # open the GUI
uv run watermark.py photo.jpg                         # GUI, image pre-loaded
uv run watermark.py --headless photo.jpg              # -> photo_watermarked.jpg
uv run watermark.py --headless photo.jpg out.png --text "Your Text" --opacity 30 --font-size 60
uv run watermark.py --headless photo.jpg --layout corner --angle 0 \
    --font-size 4% --color white --opacity 70 --outline --text "© Jane Doe"
```

**Options:**
- `input` - Image to watermark (required with `--headless`; pre-loads the GUI otherwise)
- `output` - Output file or folder (default: `<input-stem>_watermarked.<format>` next to the input); a missing extension is added from the format
- `--headless` - Write the file without opening the GUI
- `-t`, `--text` - Watermark text (default: "All rights reserved")
- `--font-size` - Text size in pixels (`60`) or percent of the shorter image side (`5%`) (default: `5%`)
- `--font` - `sans`, `serif`, `mono`, or a path to a `.ttf`/`.otf`/`.ttc` file (default: `sans`)
- `--color`, `--colour` - Text colour, as a name (`white`) or hex (`#c8c8c8`) (default: `#c8c8c8`)
- `--opacity` - Opacity percentage 0-100 (default: 30)
- `--outline` - Thin contrasting outline around the text (stands out on busy images)
- `--layout` - `tiled` (repeated over the image), `centre` (one copy) or `corner` (bottom-right) (default: `tiled`)
- `--angle` - Text angle in degrees, -90 to 90; positive tilts upwards (default: 30)
- `--spacing` - Gap between tiled copies, in multiples of the text size (default: 2)
- `-f`, `--format` - `png`, `jpg`, `webp`, `tiff` or `bmp` (default: from the output extension, else the input's format, else `png`)
- `-q`, `--quality` - JPG/WEBP quality 1-100 (default: 95)

**GUI tips:** Cmd/Ctrl+O opens an image (or click the empty preview), Cmd/Ctrl+S
or Return in the text field saves; paste a path into the image field to load
it. Opening an image sets the output name, folder and format to
`<name>_watermarked` next to it. You're asked before an existing file is
replaced, and "Show in Finder" (or "Show in folder") appears after saving.
In headless mode, an existing output file is overwritten.

**Tests:**
```bash
uv run python -m unittest tests.test_watermark -v
```

**Dependencies:** Pillow; GUI only: customtkinter and tkinter (ships with most Python installs)

---

### pdf_to_txt.py
Convert a PDF into a clean plain-text file that is easy for a large
language model to read. Preserves reading order and paragraph structure,
and marks page boundaries with `--- Page N ---` lines (disable with
`--plain`). Scanned PDFs without an OCR text layer produce a warning.

**Usage:**
```bash
uv run pdf_to_txt.py document.pdf
uv run pdf_to_txt.py document.pdf -o document.txt
uv run pdf_to_txt.py document.pdf --start-page 2 --end-page 10
```

**Options:**
- `input` - Path to the input PDF file (required)
- `-o`, `--output` - Output `.txt` path (default: `<input-stem>.txt` next to the PDF)
- `--start-page` - First page to extract, 1-based (default: 1)
- `--end-page` - Last page to extract, inclusive, 1-based (default: last page)
- `--plain` - Omit the `--- Page N ---` markers

**Dependencies:** pymupdf

---

### pdf_to_esp.py
Convert PDF files into EPS (Encapsulated PostScript) files. Accepts a single
PDF or a folder of PDFs; each page is written as a separate EPS file placed
next to the source PDF. Requires Ghostscript (`brew install ghostscript`).

**Usage:**
```bash
uv run pdf_to_esp.py document.pdf
uv run pdf_to_esp.py /path/to/folder/with/pdfs
uv run pdf_to_esp.py document.pdf --gs-path /opt/homebrew/bin/gs --dpi 600
```

**Options:**
- `input` - Path to a PDF file or a folder containing PDF files (required)
- `--gs-path` - Path to the Ghostscript executable (default: `gs` from PATH)
- `--dpi` - Rendering resolution in dots per inch (default: 1000)

**Dependencies:** pypdf, Ghostscript (system binary)

---

### latexdiff_track_changes.py
Wrapper around [`latexdiff`](https://www.ctan.org/pkg/latexdiff) — the LaTeX
equivalent of Microsoft Word's *Track Changes*. Give it an old and a new
`.tex` file and it produces a diff document where deletions are struck through
and additions are underlined/coloured. Optionally compiles the diff straight
to a PDF.

Requires `latexdiff` (ships with TeX Live / MacTeX; otherwise
`tlmgr install latexdiff` or `sudo apt install latexdiff`). `--compile` also
needs a LaTeX engine (`latexmk` by default).

**Usage:**
```bash
uv run latexdiff_track_changes.py old.tex new.tex
uv run latexdiff_track_changes.py old.tex new.tex -o changes.tex
uv run latexdiff_track_changes.py old.tex new.tex --flatten --compile
uv run latexdiff_track_changes.py old.tex new.tex --type CFONT
uv run latexdiff_track_changes.py old.tex new.tex -- --math-markup=whole
```

**Options:**
- `old` - Path to the OLD version of the LaTeX document (required)
- `new` - Path to the NEW version of the LaTeX document (required)
- `-o`, `--output` - Path for the generated diff `.tex` (default: `<new-stem>_diff.tex` next to the new file)
- `--flatten` - Expand `\input`/`\include` and `\bibliography` before diffing (multi-file projects)
- `--type` - latexdiff markup style, e.g. `UNDERLINE` (default), `CFONT`, `CULINECHBAR`, `CCHANGEBAR`
- `--subtype` - latexdiff markup subtype, e.g. `SAFE` (default), `ZLABEL`, `DVIPSCOL`
- `--encoding` - Character encoding of the input files
- `--latexdiff-path` - Name or path of the `latexdiff` executable (default: `latexdiff`)
- `--latexdiff-arg` - Extra raw argument forwarded to latexdiff (repeatable)
- `--compile` - Also compile the generated diff `.tex` into a PDF
- `--compiler` - LaTeX build tool used with `--compile` (default: `latexmk`)
- Arguments after `--` are forwarded verbatim to `latexdiff`

**Dependencies:** latexdiff (system binary), a LaTeX engine for `--compile`

---

### qr_code_generator.py
Generate a QR code from a link (or any text). By default it opens a small GUI
where you paste the link, see a live preview, and choose the resolution,
colours, error correction, output format and output file name/folder.
The window is built with CustomTkinter: card layout, segmented controls, and
light/dark mode that follows the system (or set it with the Auto/Light/Dark
switch in the header). Use `--headless` to skip the window and write the file directly (scripts,
SSH sessions); without `--headless`, the command-line options pre-fill the GUI.

Raster images are exactly `--size` pixels wide with crisp, whole-pixel modules;
SVG and PDF are true vector files (the PDF page measures `size / dpi` inches).
The tool warns when the chosen colours are likely to scan poorly (low contrast
or light-on-dark).

**Usage:**
```bash
uv run qr_code_generator.py                                  # open the GUI
uv run qr_code_generator.py "https://example.com"            # GUI, link pre-filled
uv run qr_code_generator.py --headless "https://example.com" # -> qrcode.png
uv run qr_code_generator.py --headless "https://example.com" -o website.svg
uv run qr_code_generator.py --headless "https://example.com" -o code \
    --format png --size 2048 --fg "#1d3557" --bg transparent
uv run qr_code_generator.py --headless "https://example.com" -o flyer.pdf \
    --size 1200 --dpi 300 --error-correction H
```

**Options:**
- `data` - Link or text to encode (required with `--headless`; pre-fills the GUI otherwise)
- `--headless` - Generate the file without opening the GUI
- `-o`, `--output` - Output file name or path; a missing extension is added from `--format` (default: `qrcode.<format>` in the current folder)
- `-f`, `--format` - `png`, `jpg`, `bmp`, `tiff`, `webp`, `svg` or `pdf` (default: taken from the `--output` extension, else `png`)
- `-s`, `--size` - Image width/height in pixels (default: 1024)
- `--dpi` - Print resolution stored in raster files; also sets the PDF page size (default: 300)
- `--fg`, `--color` - QR colour, as a name (`navy`) or hex (`#1d3557`, `#1d355780` with alpha) (default: `#000000`)
- `--bg`, `--background` - Background colour, or `transparent` (png/tiff/webp/svg/pdf only) (default: `#ffffff`)
- `-e`, `--error-correction` - `L` (~7%), `M` (~15%), `Q` (~25%) or `H` (~30%) of the code can be damaged or covered and still scan (default: `M`)
- `--border` - Quiet-zone width in modules; 4 is the QR standard (default: 4)

**GUI tips:** Return or Cmd/Ctrl+S saves; click a colour swatch to open the
colour picker; the format selector decides the file extension; you're asked
before an existing file is replaced, and "Show in Finder" (or "Show in folder")
appears after saving. In headless mode, an existing file is overwritten.

**Tests:**
```bash
uv run python -m unittest discover -s tests -v
# also decode every generated file back with OpenCV (test-only extra):
uv run --with opencv-python-headless python -m unittest discover -s tests -v
```

**Dependencies:** qrcode, Pillow, reportlab (PDF output); GUI only: customtkinter and tkinter (ships with most Python installs)
