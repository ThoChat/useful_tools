#!/usr/bin/env uv run
"""Merge PDF and SVG files into a single PDF (SVGs are converted to PDF).

Usage:
  uv run merge_pdfs.py output.pdf input1.pdf input2.svg ...
"""

import io
import logging
import sys
from pypdf import PdfWriter

logging.getLogger("svglib").addHandler(logging.NullHandler())
logging.getLogger("svglib").propagate = False

PDF_MAGIC = b"%PDF"


def is_actual_pdf(path):
    with open(path, "rb") as f:
        return f.read(4) == PDF_MAGIC


def convert_svg_to_pdf(svg_path):
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    drawing = svg2rlg(svg_path)
    buf = io.BytesIO()
    renderPDF.drawToFile(drawing, buf)
    buf.seek(0)
    return buf


def merge_pdfs(input_files, output_file):
    writer = PdfWriter()
    for f in input_files:
        if is_actual_pdf(f):
            print(f"Adding: {f}")
            writer.append(f)
        else:
            print(f"Converting: {f}")
            pdf_stream = convert_svg_to_pdf(f)
            writer.append(pdf_stream)
    writer.write(output_file)
    writer.close()
    print(f"\nMerged {len(input_files)} file(s) into: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    merge_pdfs(sys.argv[2:], sys.argv[1])
