#!/usr/bin/env python3
"""Convert PDF files to EPS (Encapsulated PostScript) files.

Each page of a PDF is written as a separate EPS file because EPS is a
single-page format. The output EPS files are placed next to the source PDF
in the same directory, keeping the original PDF untouched.

Accepts either a single PDF file or a folder; in the folder case every
``*.pdf`` file inside it is converted.

Usage:
  uv run pdf_to_esp.py document.pdf
  uv run pdf_to_esp.py /path/to/folder/with/pdfs
  uv run pdf_to_esp.py document.pdf --gs-path /opt/homebrew/bin/gs
  uv run pdf_to_esp.py /path/to/folder --dpi 600
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader

# Ghostscript device that emits Encapsulated PostScript (single-page EPS).
GS_EPS_DEVICE = "eps2write"


def find_pdf_files(target: str) -> list[Path]:
    """Resolve the input target into a list of PDF files.

    Args:
        target: Path to a single PDF file or to a directory containing PDFs.

    Returns:
        Sorted list of Path objects pointing to PDF files.

    Raises:
        FileNotFoundError: If *target* does not exist.
        ValueError: If *target* is neither a PDF file nor a directory.
    """
    path = Path(target).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Input not found: {target}")

    if path.is_file():
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Not a PDF file: {target}")
        return [path]

    if path.is_dir():
        pdfs = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        if not pdfs:
            raise ValueError(f"No PDF files found in directory: {target}")
        return pdfs

    raise ValueError(f"Unsupported input type: {target}")


def convert_pdf_to_eps(pdf_path: Path, gs_path: str = "gs", dpi: int = 1000) -> list[Path]:
    """Convert every page of a single PDF into EPS files.

    Uses Ghostscript's ``eps2write`` device. Each page is rendered as its own
    EPS file named ``<stem>_page_<n>.eps`` placed next to the source PDF.

    Args:
        pdf_path: Path to the input PDF file.
        gs_path: Path to the Ghostscript executable (or name resolvable
            via PATH).
        dpi: Resolution in dots per inch used for rendering vector content.

    Returns:
        List of Path objects for the EPS files that were created.

    Raises:
        RuntimeError: If Ghostscript is unavailable or fails to convert.
    """
    if shutil.which(gs_path) is None:
        raise RuntimeError(
            f"Ghostscript executable not found: '{gs_path}'. "
            "Install Ghostscript (e.g. 'brew install ghostscript') or pass --gs-path."
        )

    # Probe the number of pages so we can emit one EPS per page.
    page_count = len(PdfReader(str(pdf_path)).pages)

    output_files: list[Path] = []
    for page_num in range(1, page_count + 1):
        # EPS is single-page, so a multi-page PDF yields one file per page.
        out_path = pdf_path.with_name(f"{pdf_path.stem}_page_{page_num}.eps")
        command = [
            gs_path,
            "-dNOPAUSE",
            "-dBATCH",
            "-dSAFER",
            "-sDEVICE=" + GS_EPS_DEVICE,
            f"-dFirstPage={page_num}",
            f"-dLastPage={page_num}",
            f"-r{dpi}",
            f"-sOutputFile={out_path}",
            str(pdf_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not out_path.is_file():
            raise RuntimeError(
                f"Ghostscript failed on page {page_num} of {pdf_path.name}:\n{result.stderr}"
            )
        output_files.append(out_path)
        print(f"  Created: {out_path}")

    return output_files


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert PDF files to EPS files. Accepts a single PDF or a folder "
            "of PDFs; EPS files are written next to the source PDFs."
        )
    )
    parser.add_argument(
        "input",
        help="Path to a PDF file or a folder containing PDF files",
    )
    parser.add_argument(
        "--gs-path",
        default="gs",
        help="Path to the Ghostscript executable (default: 'gs' from PATH)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=1000,
        help="Rendering resolution in dots per inch (default: 1000)",
    )
    args = parser.parse_args()

    try:
        pdf_files = find_pdf_files(args.input)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    total_files = 0
    for pdf in pdf_files:
        print(f"Converting: {pdf}")
        try:
            created = convert_pdf_to_eps(pdf, args.gs_path, args.dpi)
            total_files += len(created)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f"\nConverted {len(pdf_files)} PDF(s) into {total_files} EPS file(s).")


if __name__ == "__main__":
    main()
