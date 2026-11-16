"""Check what ``bench-setup services --compose`` left behind, for real.

    python tests/e2e/check.py intake parts          # these two up and wired, recon off
    python tests/e2e/check.py intake parts recon    # all three

For each named service: the right app answers /healthz on its port, and the
stand-in benchhud's config has its URL and a token. For each one not named:
nothing answers on its port and the config has no section for it.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from bench_setup.host import Host
from bench_setup.services import SERVICES


def main(wanted: set[str]) -> int:
    config_path = Path(os.environ["BENCH_E2E_CONFIG"])
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    host = Host()
    problems: list[str] = []
    for service in SERVICES:
        body = host.healthz(service.default_port)
        section = config.get(service.section)
        if service.compose_name in wanted:
            if not body or body.get("app") != service.name:
                problems.append(f"{service.name}: /healthz on {service.default_port} gave {body}")
            url = f"http://127.0.0.1:{service.default_port}"
            if not section or section.get("base_url") != url or not section.get("token"):
                problems.append(f"{service.name}: benchhud's [{service.section}] isn't wired")
        else:
            if body is not None:
                problems.append(f"{service.name}: something answers on {service.default_port}")
            if section is not None:
                problems.append(f"{service.name}: benchhud has a [{service.section}] section")
    for problem in problems:
        print(problem, file=sys.stderr)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:])))
