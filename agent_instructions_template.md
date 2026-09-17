Instructions to create a new tool in this collection

## Context
This is a Python toolbox composed of diverse everyday tools. Each tool is a standalone script invoked via `uv run`. The project uses `uv` for dependency management and targets Python 3.13. New tools should follow the existing conventions: a single self-contained script with a clear docstring, CLI arguments via `argparse`, and a `main()` entry point. After creating a tool, update `README.md` and `pyproject.toml` if new dependencies are needed.

## Code Quality Requirements
- All code must be well documented with clear docstrings and comments explaining the logic.
- Follow PEP 8 style.
- Each function should have a docstring describing its purpose, parameters, and return values.
- The script should include a module-level docstring with usage examples.
- the code should be tested using unit test you create as when as explicit test stated in the "Testing section" of these instructions

## After creating the tool:
- Update `README.md` with a section for the new tool, including a brief description, usage, and options.
- Update `pyproject.toml` if new dependencies are required.

## Tool Description
...


## Testing 
N.A