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
Add a diagonal text watermark to an image.

**Usage:**
```bash
uv run watermark.py input_name.png output_name.png --text "Your Text" --opacity 30 --font-size 60
```

**Options:**
- `input` - Input image path (required)
- `output` - Output image path (required)
- `--text` - Watermark text (default: "All rights reserved")
- `--opacity` - Opacity percentage 0-100 (default: 30)
- `--font-size` - Font size (default: 60)

**Dependencies:** Pillow (PIL)
