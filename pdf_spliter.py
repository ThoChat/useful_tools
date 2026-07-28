#!/usr/bin/env python3
"""Split a PDF into individual pages, each saved as a separate PDF file.

Creates a folder named after the input PDF (without extension) in the specified
output directory, containing one PDF per page of the original document.

Usage:
  uv run pdf_spliter.py input.pdf
  uv run pdf_spliter.py input.pdf --output-dir /path/to/output
  uv run pdf_spliter.py input.pdf --output-dir /path/to/output --digits 4
"""

import argparse
import os
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def split_pdf(input_path: str, output_dir: str, digits: int = 3) -> str:
    """Split a multi-page PDF into individual page PDFs.

    Creates a folder named after the input PDF (sans extension) inside
    *output_dir*, and writes one PDF file per page inside it.

    Args:
        input_path: Path to the input PDF file.
        output_dir: Parent directory where the page-output folder will be
            created.
        digits: Number of zero-padded digits used in page filenames
            (default 3 → ``page_001.pdf``).

    Returns:
        Absolute path to the created output folder.

    Raises:
        FileNotFoundError: If *input_path* does not exist.
        ValueError: If the PDF has zero pages.
    """
    src = Path(input_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    reader = PdfReader(src)
    if len(reader.pages) == 0:
        raise ValueError(f"PDF has no pages: {input_path}")

    folder_name = src.stem
    out_folder = Path(output_dir).resolve() / folder_name
    out_folder.mkdir(parents=True, exist_ok=True)

    for i, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        page_path = out_folder / f"page_{i:0{digits}d}.pdf"
        with open(page_path, "wb") as f:
            writer.write(f)
        print(f"  Created: {page_path}")

    return str(out_folder)


def main():
    parser = argparse.ArgumentParser(
        description="Split a PDF into one file per page."
    )
    parser.add_argument("input", help="Path to the input PDF file")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to create the output folder in (default: current directory)",
    )
    parser.add_argument(
        "--digits",
        type=int,
        default=3,
        help="Number of zero-padded digits in page filenames (default: 3)",
    )
    args = parser.parse_args()

    try:
        out_folder = split_pdf(args.input, args.output_dir, args.digits)
        print(f"\nAll pages extracted to: {out_folder}")
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
