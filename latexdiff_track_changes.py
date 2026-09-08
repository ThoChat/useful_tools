#!/usr/bin/env python3
"""Generate a "Track Changes" LaTeX document from two versions of a document.

This is a thin, convenience-focused wrapper around ``latexdiff``
(https://www.ctan.org/pkg/latexdiff), the LaTeX equivalent of Microsoft
Word's *Track Changes* feature. Given an *old* and a *new* ``.tex`` file it
produces a third ``.tex`` file in which deletions are struck through and
additions are underlined/coloured. Optionally the resulting diff document is
compiled straight to a PDF.

``latexdiff`` itself is a Perl script and must be installed separately, e.g.::

  # TeX Live ships it by default; otherwise:
  tlmgr install latexdiff
  # or on Debian/Ubuntu:
  sudo apt install latexdiff
  # or on macOS with Homebrew (via MacTeX / TeX Live):
  brew install latexdiff

Usage:
  uv run latexdiff_track_changes.py old.tex new.tex
  uv run latexdiff_track_changes.py old.tex new.tex -o changes.tex
  uv run latexdiff_track_changes.py old.tex new.tex --flatten
  uv run latexdiff_track_changes.py old.tex new.tex --type CFONT --subtype ZLABEL
  uv run latexdiff_track_changes.py old.tex new.tex --compile
  uv run latexdiff_track_changes.py old.tex new.tex --latexdiff-arg=--math-markup=whole

Any option after ``--`` (or repeated ``--latexdiff-arg``) is forwarded verbatim
to ``latexdiff`` so the full power of the underlying tool stays available.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Default name of the ``latexdiff`` executable (resolved via PATH).
DEFAULT_LATEXDIFF = "latexdiff"

# Default LaTeX engine used when ``--compile`` is requested. ``latexmk`` is
# preferred because it transparently runs the required number of passes.
DEFAULT_COMPILER = "latexmk"


def resolve_tex_file(value: str) -> Path:
    """Validate a command-line path and return it as a resolved ``Path``.

    Args:
        value: Path to a ``.tex`` file provided on the command line.

    Returns:
        The absolute, resolved :class:`~pathlib.Path` to the file.

    Raises:
        argparse.ArgumentTypeError: If the path does not exist, is not a
            regular file, or does not have a ``.tex`` extension.
    """
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"File not found: {value}")
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Not a regular file: {value}")
    if path.suffix.lower() != ".tex":
        raise argparse.ArgumentTypeError(f"Not a .tex file: {value}")
    return path


def default_output_path(old: Path, new: Path) -> Path:
    """Derive a sensible default path for the diff document.

    The file is written next to the *new* document and named
    ``<new-stem>_diff.tex`` so repeated runs stay grouped with the source.

    Args:
        old: Path to the old ``.tex`` file (unused, kept for symmetry/clarity).
        new: Path to the new ``.tex`` file.

    Returns:
        The :class:`~pathlib.Path` where the diff document should be written.
    """
    return new.with_name(f"{new.stem}_diff.tex")


def build_latexdiff_command(
    latexdiff_path: str,
    old: Path,
    new: Path,
    *,
    flatten: bool,
    markup_type: str | None,
    markup_subtype: str | None,
    encoding: str | None,
    extra_args: list[str],
) -> list[str]:
    """Assemble the ``latexdiff`` command line.

    Args:
        latexdiff_path: Name or path of the ``latexdiff`` executable.
        old: Path to the old ``.tex`` file.
        new: Path to the new ``.tex`` file.
        flatten: If ``True``, pass ``--flatten`` so ``\\input``/``\\include``
            files and ``\\bibliography`` are expanded before diffing.
        markup_type: Value for ``latexdiff``'s ``--type`` option (the visual
            markup style, e.g. ``UNDERLINE``, ``CFONT``, ``CULINECHBAR``).
            ``None`` leaves the ``latexdiff`` default.
        markup_subtype: Value for ``latexdiff``'s ``--subtype`` option (how
            changed blocks are delimited, e.g. ``SAFE``, ``ZLABEL``).
        encoding: Value for ``latexdiff``'s ``--encoding`` option, or ``None``.
        extra_args: Additional raw arguments forwarded to ``latexdiff``.

    Returns:
        The command as a list of string tokens ready for :func:`subprocess.run`.
    """
    command: list[str] = [latexdiff_path]

    if flatten:
        command.append("--flatten")
    if markup_type:
        command.append(f"--type={markup_type}")
    if markup_subtype:
        command.append(f"--subtype={markup_subtype}")
    if encoding:
        command.append(f"--encoding={encoding}")

    command.extend(extra_args)

    # Positional arguments must come last: old file first, then new file.
    command.extend([str(old), str(new)])
    return command


def run_latexdiff(command: list[str], output_path: Path) -> None:
    """Run ``latexdiff`` and write its stdout to *output_path*.

    ``latexdiff`` prints the annotated LaTeX source to standard output, so the
    wrapper is responsible for capturing it and saving it to a file.

    Args:
        command: The command produced by :func:`build_latexdiff_command`.
        output_path: File to which the diff document is written.

    Raises:
        RuntimeError: If ``latexdiff`` exits with a non-zero status or
            produces no output.
    """
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            "latexdiff failed (exit code "
            f"{result.returncode}):\n{result.stderr.strip()}"
        )
    if not result.stdout:
        raise RuntimeError(
            "latexdiff produced no output.\n"
            f"stderr:\n{result.stderr.strip()}"
        )

    output_path.write_text(result.stdout, encoding="utf-8")

    # latexdiff emits diagnostics (package detection, warnings) on stderr even
    # on success; surface them so the user can act on them.
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def compile_pdf(tex_path: Path, compiler: str) -> Path:
    """Compile *tex_path* into a PDF in the same directory.

    Args:
        tex_path: Path to the ``.tex`` file to compile.
        compiler: Name/path of the LaTeX build tool. ``latexmk`` is used with
            ``-pdf``; any other value is invoked like ``pdflatex`` with
            ``-interaction=nonstopmode``.

    Returns:
        Path to the generated PDF file.

    Raises:
        RuntimeError: If the compiler is missing or the build fails to yield
            a PDF.
    """
    if shutil.which(compiler) is None:
        raise RuntimeError(
            f"LaTeX compiler not found: '{compiler}'. Install it or pass "
            "--compiler with the correct name/path."
        )

    workdir = tex_path.parent
    if Path(compiler).name.startswith("latexmk"):
        command = [
            compiler,
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex_path.name,
        ]
    else:
        command = [
            compiler,
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex_path.name,
        ]

    result = subprocess.run(
        command, cwd=workdir, capture_output=True, text=True
    )

    pdf_path = tex_path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf_path.is_file():
        # LaTeX writes the useful error context to stdout, not stderr.
        raise RuntimeError(
            f"Compilation of {tex_path.name} failed.\n"
            f"{result.stdout.strip()[-2000:]}"
        )
    return pdf_path


def main():
    """Parse arguments, run ``latexdiff``, and optionally compile the result."""
    parser = argparse.ArgumentParser(
        description=(
            "Wrapper around latexdiff: produce a 'Track Changes' LaTeX "
            "document (and optionally a PDF) from an old and a new .tex file."
        )
    )
    parser.add_argument(
        "old",
        type=resolve_tex_file,
        help="Path to the OLD version of the LaTeX document",
    )
    parser.add_argument(
        "new",
        type=resolve_tex_file,
        help="Path to the NEW version of the LaTeX document",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Path for the generated diff .tex file "
            "(default: <new-stem>_diff.tex next to the new file)"
        ),
    )
    parser.add_argument(
        "--flatten",
        action="store_true",
        help=(
            "Expand \\input/\\include and \\bibliography before diffing "
            "(needed for multi-file projects)"
        ),
    )
    parser.add_argument(
        "--type",
        dest="markup_type",
        help=(
            "latexdiff markup style, e.g. UNDERLINE (default), CFONT, "
            "CULINECHBAR, CCHANGEBAR"
        ),
    )
    parser.add_argument(
        "--subtype",
        dest="markup_subtype",
        help=(
            "latexdiff markup subtype controlling change delimiters, "
            "e.g. SAFE (default), ZLABEL, DVIPSCOL"
        ),
    )
    parser.add_argument(
        "--encoding",
        help="Character encoding of the input files (passed to latexdiff)",
    )
    parser.add_argument(
        "--latexdiff-path",
        default=DEFAULT_LATEXDIFF,
        help="Name or path of the latexdiff executable (default: latexdiff)",
    )
    parser.add_argument(
        "--latexdiff-arg",
        action="append",
        default=[],
        metavar="ARG",
        help=(
            "Extra raw argument forwarded to latexdiff; repeat for multiple, "
            "e.g. --latexdiff-arg=--math-markup=whole"
        ),
    )
    parser.add_argument(
        "--compile",
        action="store_true",
        help="Also compile the generated diff .tex into a PDF",
    )
    parser.add_argument(
        "--compiler",
        default=DEFAULT_COMPILER,
        help=(
            "LaTeX build tool used with --compile (default: latexmk); "
            "any pdflatex-compatible engine name also works"
        ),
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="Arguments after '--' are forwarded verbatim to latexdiff",
    )
    args = parser.parse_args()

    # Ensure latexdiff is available before doing any work.
    if shutil.which(args.latexdiff_path) is None:
        print(
            f"Error: latexdiff executable not found: '{args.latexdiff_path}'.\n"
            "Install it (e.g. 'tlmgr install latexdiff' or "
            "'sudo apt install latexdiff') or pass --latexdiff-path.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Merge the two ways of passing pass-through arguments. argparse.REMAINDER
    # keeps a leading '--', so drop it if present.
    forwarded = list(args.latexdiff_arg)
    if args.extra:
        tail = args.extra[1:] if args.extra[0] == "--" else args.extra
        forwarded.extend(tail)

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path(args.old, args.new)
    )

    command = build_latexdiff_command(
        args.latexdiff_path,
        args.old,
        args.new,
        flatten=args.flatten,
        markup_type=args.markup_type,
        markup_subtype=args.markup_subtype,
        encoding=args.encoding,
        extra_args=forwarded,
    )

    print(f"Running: {' '.join(command)}")
    try:
        run_latexdiff(command, output_path)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Diff document written to: {output_path}")

    if args.compile:
        print(f"Compiling {output_path.name} with {args.compiler} ...")
        try:
            pdf_path = compile_pdf(output_path, args.compiler)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"PDF written to: {pdf_path}")


if __name__ == "__main__":
    main()
