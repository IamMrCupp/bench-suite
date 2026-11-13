"""The ``bench-setup`` command line.

Standard library only, on purpose: it runs on a fresh host before anything
else is installed, and ships in a ``.deb`` that depends on the system
``python3`` alone. The subcommands (services, host, cameras, doctor) land in
their own changes; this is the entry point they hang off.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from bench_setup import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench-setup",
        description=(
            "Set up the bench: install benchhud-intake, bench-parts and bench-recon, "
            "and point benchhud at them."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"bench-setup {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    # No subcommands yet: say so plainly instead of pretending to do something.
    print("bench-setup: nothing to do yet. Run bench-setup --help.")
    return 0
