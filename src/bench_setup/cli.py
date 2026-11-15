"""The ``bench-setup`` command line.

Standard library only, on purpose: it runs on a fresh host before anything
else is installed, and ships in a ``.deb`` that depends on the system
``python3`` alone.

    sudo bench-setup services                    # set up the services, wire benchhud
    sudo bench-setup services --recon            # ...and turn bench-recon on
    printf '%s\\n' "$PW" | sudo bench-setup services --non-interactive

The other subcommands (host, cameras, doctor) land in their own changes.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from bench_setup import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench-setup",
        description=(
            "Set up the bench: turn on benchhud-intake, bench-parts and bench-recon, "
            "and point benchhud at them."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"bench-setup {__version__}")
    sub = parser.add_subparsers(dest="command")

    services = sub.add_parser(
        "services",
        help="set the password, make the tokens, start the services, wire benchhud",
        description=(
            "Set one password on the installed bench services, make each one's API token, "
            "start them, and write their URLs and tokens into benchhud's config. "
            "Run it again any time: it only changes what's missing."
        ),
    )
    services.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; read the password from one line of stdin if a service needs one",
    )
    recon = services.add_mutually_exclusive_group()
    recon.add_argument(
        "--recon",
        dest="recon",
        action="store_const",
        const=True,
        help="turn bench-recon on without asking (it holds client engagement data)",
    )
    recon.add_argument(
        "--no-recon",
        dest="recon",
        action="store_const",
        const=False,
        help="leave bench-recon off without asking",
    )
    services.add_argument(
        "--user",
        help="whose benchhud to wire (default: the account that ran sudo)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "services":
        from bench_setup import services

        return services.run(
            services.Options(interactive=not args.non_interactive, recon=args.recon, user=args.user)
        )
    # The full guided run isn't here yet: say so plainly instead of pretending.
    print("bench-setup: nothing to do yet. Try: sudo bench-setup services")
    return 0
