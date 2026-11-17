"""The ``bench-setup`` command line.

Standard library only, on purpose: it runs on a fresh host before anything
else is installed, and ships in a ``.deb`` that depends on the system
``python3`` alone.

    sudo bench-setup                             # every step below, in order, asking as it goes

    sudo bench-setup services                    # set up the services, wire benchhud
    sudo bench-setup services --recon            # ...and turn bench-recon on
    printf '%s\\n' "$PW" | sudo bench-setup services --non-interactive
    bench-setup services --compose               # the same, for the containers
    sudo bench-setup host                        # virtual camera, torn frames, two cards
    sudo bench-setup cameras                     # /dev/cam-* names that don't move
    bench-setup obs                              # what to point OBS at
    bench-setup doctor                           # look, report, change nothing
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from bench_setup import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench-setup",
        description=(
            "Set up the bench: turn on benchhud-intake, bench-parts and bench-recon, point "
            "benchhud at them, and get the host ready. With no command it runs every step "
            "in order, asking as it goes: the services, the host, the camera names, what to "
            "tell OBS, and a check at the end. Each step is also a command on its own."
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
        "--compose",
        nargs="?",
        const="",
        metavar="FILE",
        help=(
            "set up the containers from a compose file instead of the installed packages; "
            "FILE defaults to ./compose.yaml, then the one bench-suite installs. Needs no sudo"
        ),
    )
    services.add_argument(
        "--user",
        help="whose benchhud to wire (default: the account that ran sudo)",
    )

    doctor = sub.add_parser(
        "doctor",
        help="check the services and the host, and change nothing",
        description=(
            "Check that the bench services are on, answering, listening only on this machine "
            "and wired into benchhud, and that the host is set up the way benchhud's "
            "BENCH-SETUP.md describes. Changes nothing. With sudo it can also check the "
            "passwords and tokens (it never prints them). Exits 1 if something is broken."
        ),
    )
    doctor.add_argument(
        "--user",
        help="whose benchhud to look at (default: the account that ran it)",
    )

    host = sub.add_parser(
        "host",
        help="set up the virtual camera node and the kernel settings the bench needs",
        description=(
            "Walk through the host steps from benchhud's BENCH-SETUP.md: the v4l2loopback "
            "node benchhud sends its picture to OBS through, uvcvideo dropping torn frames, "
            "the ARP settings for a bench with two network cards on one subnet, the account's "
            "groups, and letting it run uhubctl to reset a hung camera. Each step "
            "shows the file it would write and asks first. Steps that don't apply are skipped, "
            "and it's safe to run again."
        ),
    )
    host.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; without --yes this only reports what it would do",
    )
    host.add_argument("--yes", action="store_true", help="answer yes to every question")
    host.add_argument(
        "--nodes",
        type=int,
        choices=[1, 2, 4],
        help=(
            "virtual camera nodes: 1 (benchhud), 2 (plus OBS's own virtual camera) or "
            "4 (plus separate scope and thermal feeds)"
        ),
    )
    host.add_argument(
        "--undo",
        action="store_true",
        help="remove the files bench-setup host wrote, and only those",
    )
    host.add_argument("--user", help="whose benchhud to read (default: the account that ran sudo)")

    cameras = sub.add_parser(
        "cameras",
        help="give the bench cameras /dev/cam-* names that don't move",
        description=(
            "List the USB cameras plugged in, ask which is the scope and which the thermal "
            "if benchhud doesn't know, install the udev rules that give each a /dev/cam-* "
            "name, and keep the thermal camera out of USB autosuspend. Run it with every "
            "camera plugged in, and again after adding one."
        ),
    )
    cameras.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; without --yes this only reports what it would do",
    )
    cameras.add_argument("--yes", action="store_true", help="answer yes to every question")
    cameras.add_argument(
        "--user", help="whose benchhud to ask (default: the account that ran sudo)"
    )

    obs = sub.add_parser(
        "obs",
        help="say which devices to point OBS at, and check the scenes it has",
        description=(
            "List the devices the bench gives OBS by the names that hold still, and read "
            "OBS's scene collections for sources saved as /dev/videoN or pointed at a camera "
            "benchhud needs for itself. Reads only; it never edits a scene."
        ),
    )
    obs.add_argument("--user", help="whose benchhud and OBS to read (default: yours)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "services":
        from bench_setup import services

        compose: Path | None = None
        if args.compose is not None:
            compose = Path(args.compose).resolve() if args.compose else services.find_compose_file()
            if compose is None:
                looked = " or ".join(str(p) for p in services.COMPOSE_FILES)
                print(f"bench-setup: no compose file at {looked}. Name one.", file=sys.stderr)
                return services.EXIT_USAGE
        return services.run(
            services.Options(
                interactive=not args.non_interactive,
                recon=args.recon,
                user=args.user,
                compose=compose,
            )
        )
    if args.command == "doctor":
        from bench_setup import doctor

        return doctor.run(doctor.Options(user=args.user))
    if args.command == "host":
        from bench_setup import hostsetup

        return hostsetup.run(
            hostsetup.Options(
                interactive=not args.non_interactive,
                yes=args.yes,
                undo=args.undo,
                nodes=args.nodes,
                user=args.user,
            )
        )
    if args.command == "cameras":
        from bench_setup import cameras

        return cameras.run(
            cameras.Options(interactive=not args.non_interactive, yes=args.yes, user=args.user)
        )
    if args.command == "obs":
        from bench_setup import obs

        return obs.run(obs.Options(user=args.user))
    from bench_setup import guided

    return guided.run()
