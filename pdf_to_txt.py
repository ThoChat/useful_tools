#!/usr/bin/env python3
"""Convert a PDF document into a clean plain-text (.txt) file for LLM reading.

Large language models cannot consume raw PDFs directly; this tool extracts
the embedded text layer, preserves reading order and paragraph structure,
and writes the result to a plain ``.txt`` file. Page boundaries are marked
with ``--- Page N ---`` lines by default so a model can refer to specific
pages.

Only the embedded text is extracted: a scanned PDF that has no OCR pass
contains (almost) no text layer, in which case the tool prints a warning.

Usage:
  uv run pdf_to_txt.py document.pdf
  uv run pdf_to_txt.py document.pdf -o document.txt
  uv run pdf_to_txt.py document.pdf --start-page 2 --end-page 10
  uv run pdf_to_txt.py document.pdf --plain
"""

import argparse
import re
import sys
from pathlib import Path

import pymupdf as fitz  # PyMuPDF: fast PDF text extraction, preserves reading order.

# More than two consecutive newlines carry no structural meaning for a text
# model, so they are collapsed down to a single blank line.
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def _clean_page_text(text: str) -> str:
    """Normalise a single page of extracted text.

    Strips leading/trailing whitespace, removes trailing spaces on each
    line, and collapses runs of 3+ newlines into a single blank line so the
    output stays compact without destroying paragraph breaks.

    Args:
        text: Raw text extracted from one PDF page.

    Returns:
        The cleaned page text (may be empty for image-only pages).
    """
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = "\n".join(lines).strip()
    return _MULTI_NEWLINE.sub("\n\n", cleaned)


def extract_pdf_pages(
    pdf_path: Path,
    start_page: int | None = None,
    end_page: int | None = None,
) -> tuple[list[str], int]:
    """Extract the text of a page range from a PDF, one string per page.

    Args:
        pdf_path: Path to the PDF file to read.
        start_page: 1-based index of the first page to extract
            (default: first page).
        end_page: 1-based index of the last page to extract, inclusive
            (default: last page).

    Returns:
        Tuple of (pages, total_page_count) where *pages* is a list of
        cleaned strings in document order and *total_page_count* is the
        number of pages in the whole document.

    Raises:
        FileNotFoundError: If *pdf_path* does not exist.
        ValueError: If the file is not a readable PDF or the requested
            page range is out of bounds.
    """
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        # Guard against PyMuPDF's fallback of wrapping arbitrary text
        # files into a fake one-page document.
        raise ValueError(f"Not a PDF file: {pdf_path}")

    try:
        document = fitz.open(pdf_path)
    except Exception as exc:  # fitz raises various exception types here.
        raise ValueError(f"Could not open '{pdf_path}' as a PDF: {exc}") from exc

    with document:
        page_count = document.page_count

        # Validate the requested range against the actual document length.
        if start_page is not None and not 1 <= start_page <= page_count:
            raise ValueError(
                f"--start-page must be between 1 and {page_count} "
                f"(got {start_page})"
            )
        if end_page is not None and not 1 <= end_page <= page_count:
            raise ValueError(
                f"--end-page must be between 1 and {page_count} (got {end_page})"
            )
        if (
            start_page is not None
            and end_page is not None
            and end_page < start_page
        ):
            raise ValueError("--end-page must not be smaller than --start-page")

        first = (start_page or 1) - 1  # convert to 0-based indexing
        last = end_page or page_count
        pages = [_clean_page_text(document[i].get_text("text")) for i in range(first, last)]

    return pages, page_count


def build_document(pages: list[str], first_page: int, plain: bool = False) -> str:
    """Join per-page text into a single LLM-friendly document.

    Args:
        pages: Cleaned page texts in document order.
        first_page: 1-based document page number of *pages[0]*; used so
            page markers stay accurate when a sub-range was extracted.
        plain: When True, omit the ``--- Page N ---`` markers.

    Returns:
        The full document as one string with a trailing newline.
    """
    chunks: list[str] = []
    for offset, text in enumerate(pages):
        if not plain:
            # The marker gives a model an explicit, quotable page reference.
            chunks.append(f"--- Page {first_page + offset} ---\n{text}")
        else:
            chunks.append(text)
    return "\n\n".join(chunk for chunk in chunks if chunk) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert a PDF to a clean plain-text file that is easy for "
            "large language models to read."
        )
    )
    parser.add_argument("input", help="Path to the input PDF file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output .txt path (default: <input-stem>.txt next to the PDF)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        help="First page to extract, 1-based (default: 1)",
    )
    parser.add_argument(
        "--end-page",
        type=int,
        help="Last page to extract, inclusive, 1-based (default: last page)",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Omit the '--- Page N ---' markers in the output",
    )
    args = parser.parse_args()

    pdf_path = Path(args.input)
    output_path = Path(args.output) if args.output else pdf_path.with_suffix(".txt")

    try:
        pages, page_count = extract_pdf_pages(
            pdf_path, args.start_page, args.end_page
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    first_page = (args.start_page or 1) - 1
    document_text = build_document(pages, first_page + 1, plain=args.plain)

    # Scanned PDFs have no (or a nearly empty) text layer; warn instead of
    # silently writing an empty file.
    if not document_text.strip():
        print(
            "Warning: no extractable text found. The PDF may be scanned "
            "images without an OCR pass; the output file will be empty.",
            file=sys.stderr,
        )

    output_path.write_text(document_text, encoding="utf-8")
    print(
        f"Converted {pdf_path} -> {output_path} "
        f"({len(pages)} of {page_count} page(s), {len(document_text)} chars)"
    )


if __name__ == "__main__":
    main()
