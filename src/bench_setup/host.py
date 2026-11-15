"""Everything bench-setup does to the machine goes through here.

One class, so the setup logic in :mod:`bench_setup.services` can be tested
against a fake without touching systemd, the network or anyone's config.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import time
import tomllib
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: PATH for commands run as the desktop user: system locations only.
_USER_PATH = "/usr/local/bin:/usr/bin:/bin"


class Host:
    """The real machine."""

    def is_root(self) -> bool:
        return os.geteuid() == 0

    def sudo_user(self) -> str | None:
        """Who ran ``sudo bench-setup``: the desktop user benchhud belongs to."""
        name = os.environ.get("SUDO_USER", "").strip()
        return name if name and name != "root" else None

    def user_exists(self, user: str) -> bool:
        try:
            pwd.getpwnam(user)
        except KeyError:
            return False
        return True

    def installed(self, command: str) -> bool:
        return shutil.which(command) is not None

    def read_settings(self, path: Path) -> dict[str, str]:
        """``KEY=value`` pairs from a service's settings.env; missing counts as empty.

        The same format the services write and systemd reads as an
        EnvironmentFile: blank lines and ``#`` comments are skipped.
        """
        try:
            text = path.read_text("utf-8")
        except (FileNotFoundError, PermissionError):
            return {}
        values: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        return values

    def run(self, cmd: Sequence[str], stdin: str | None = None) -> tuple[int, str, str]:
        """Run ``cmd`` as root. Returns ``(exit code, stdout, stderr)``.

        Secrets go in on ``stdin`` and come back on stdout; never in ``cmd``,
        which anyone can read in ``ps``.
        """
        done = subprocess.run(list(cmd), input=stdin, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout, done.stderr

    def run_as_user(
        self, user: str, cmd: Sequence[str], stdin: str | None = None
    ) -> tuple[int, str, str]:
        """Run ``cmd`` as the desktop user, with that user's HOME and a clean environment.

        benchhud finds its config through HOME, so root's environment must not
        leak in: the file would land in /root, or be created root-owned.
        """
        entry = pwd.getpwnam(user)
        env = {
            "HOME": entry.pw_dir,
            "USER": user,
            "LOGNAME": user,
            "PATH": _USER_PATH,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        done = subprocess.run(
            list(cmd),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            user=entry.pw_uid,
            group=entry.pw_gid,
            extra_groups=os.getgrouplist(user, entry.pw_gid),
            env=env,
            cwd=entry.pw_dir,
        )
        return done.returncode, done.stdout, done.stderr

    def unit_enabled(self, unit: str) -> bool:
        return self.run(["systemctl", "is-enabled", "--quiet", unit])[0] == 0

    def unit_active(self, unit: str) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", unit])[0] == 0

    def systemctl(self, *args: str) -> bool:
        return self.run(["systemctl", *args])[0] == 0

    def healthz(self, port: int) -> dict[str, Any] | None:
        """The service's ``/healthz`` body on loopback, or None if nothing answers."""
        # No proxies: this is 127.0.0.1, and a proxy from the environment would
        # send the request somewhere else.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def benchhud_running(self, user: str) -> bool:
        return self.run(["pgrep", "-u", user, "-x", "benchhud"])[0] == 0

    def read_toml(self, path: Path) -> dict[str, Any]:
        """benchhud's config as a dict; missing or unreadable counts as empty."""
        try:
            with path.open("rb") as fh:
                return tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
