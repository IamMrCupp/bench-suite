"""``bench-setup services``: turn the services on and point benchhud at them.

The deb route. apt has already installed benchhud-intake, bench-parts and
bench-recon; this sets one password on them, makes each one's API token,
starts them, checks the right app answers on each port, and writes the URLs
and tokens into benchhud's config.

Three rules shape everything here:

- **A re-run changes only what's missing.** A service that has a password keeps
  it. A service that has a token keeps it, since rotating one would cut off
  anything else using it.
- **Secrets travel over pipes.** Passwords and tokens go in on stdin and come
  back on stdout. Never in argv (``ps`` shows it), never in output.
- **bench-recon stays off unless the owner says yes.** It holds client
  engagement data.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bench_setup.host import Host

#: Exit codes. 2 matches argparse's "you ran it wrong".
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: How long a just-started service gets to answer /healthz.
HEALTH_TRIES = 30
HEALTH_WAIT = 1.0

PASSWORD_TRIES = 3


@dataclass(frozen=True)
class Service:
    """One bench service, as its .deb installs it.

    ``name`` is the package, the command, the systemd unit and the app name on
    ``/healthz``: the three services keep those the same on purpose.
    """

    name: str
    env_prefix: str
    default_port: int
    section: str  # benchhud's config section
    holds: str  # what's in it, for the messages
    dormant: bool = False  # installed off; the owner turns it on

    @property
    def settings_path(self) -> Path:
        return Path(f"/var/lib/{self.name}/settings.env")

    @property
    def password_key(self) -> str:
        return f"{self.env_prefix}_PASSWORD_HASH"

    @property
    def token_key(self) -> str:
        return f"{self.env_prefix}_API_TOKEN"

    @property
    def port_key(self) -> str:
        return f"{self.env_prefix}_PORT"


SERVICES: tuple[Service, ...] = (
    Service("benchhud-intake", "INTAKE", 8401, "intake", "customer records"),
    Service("bench-parts", "BENCH_PARTS", 8402, "inventory", "parts stock"),
    Service("bench-recon", "BENCHRECON", 8403, "recon", "client engagement data", dormant=True),
)


@dataclass(frozen=True)
class Options:
    interactive: bool = True
    #: True/False from --recon/--no-recon; None asks (or leaves it off, non-interactive).
    recon: bool | None = None
    #: The desktop user benchhud belongs to; None means whoever ran sudo.
    user: str | None = None


class UI(Protocol):
    def say(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def ask_password(self) -> str | None: ...
    def ask_yes_no(self, question: str) -> bool: ...


class TerminalUI:
    """Prompts on the terminal, or reads the password from stdin when scripted."""

    def __init__(self, interactive: bool) -> None:
        self.interactive = interactive

    def say(self, message: str) -> None:
        print(message)

    def warn(self, message: str) -> None:
        print(message, file=sys.stderr)

    def ask_password(self) -> str | None:
        if not self.interactive:
            line = sys.stdin.readline().removesuffix("\n").removesuffix("\r")
            return line if line.strip() else None
        for _ in range(PASSWORD_TRIES):
            first = getpass.getpass("Password for the bench services: ")
            if not first.strip():
                self.warn("The password can't be blank.")
                continue
            if first != getpass.getpass("Again: "):
                self.warn("The passwords didn't match.")
                continue
            return first
        return None

    def ask_yes_no(self, question: str) -> bool:
        if not self.interactive:
            return False
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


class _Failed(Exception):
    """One service couldn't be set up. The message is safe to print."""


class _Run:
    """One pass over the services."""

    def __init__(self, host: Host, ui: UI, options: Options) -> None:
        self.host = host
        self.ui = ui
        self.options = options
        self.user = options.user or host.sudo_user()
        self._password: str | None = None
        self._password_asked = False
        self._config_path: Path | None = None
        self._benchhud_note_shown = False

    # -- the pass ---------------------------------------------------------

    def go(self) -> int:
        failed = False
        seen = False
        for service in SERVICES:
            if not self.host.installed(service.name):
                self.ui.say(f"{service.name}: not installed (sudo apt install {service.name})")
                continue
            seen = True
            try:
                self._set_up(service)
            except _Failed as exc:
                failed = True
                self.ui.warn(f"{service.name}: {exc}")
        if not seen:
            self.ui.warn(
                "None of the bench services are installed. Try: sudo apt install bench-suite"
            )
            return EXIT_FAILED
        return EXIT_FAILED if failed else EXIT_OK

    def _set_up(self, service: Service) -> None:
        host = self.host
        enabled = host.unit_enabled(service.name)
        if service.dormant and not enabled and not self._turn_on(service):
            self.ui.say(f"{service.name}: installed, off")
            return

        settings = host.read_settings(service.settings_path)
        changed = False

        if not settings.get(service.password_key):
            self._run_cli(
                service, ["set-password", "--stdin"], stdin=self._shared_password() + "\n"
            )
            changed = True

        token = settings.get(service.token_key, "")
        if not token:
            token = self._run_cli(service, ["new-token", "--print"]).strip()
            if not token:
                raise _Failed("new-token --print gave no token")
            changed = True

        port = self._port(service, settings)
        self._start(service, enabled=enabled, changed=changed)
        self._wait_for(service, port)
        wired = self._wire_benchhud(service, port, token)

        state = "set up" if changed else "already set up"
        self.ui.say(f"{service.name}: {state}, on 127.0.0.1:{port}{wired}")

    # -- steps ------------------------------------------------------------

    def _turn_on(self, service: Service) -> bool:
        if self.options.recon is not None:
            return self.options.recon
        if not self.options.interactive:
            return False  # a script turns it on with --recon, never by default
        return self.ui.ask_yes_no(
            f"Turn on {service.name}? It holds {service.holds}; it stays on this machine only."
        )

    def _shared_password(self) -> str:
        """Asked for once, the first time a service needs it. Never stored."""
        if self._password is None:
            # Asked once per run, even when it fails: the next service gets the
            # same answer instead of another round of prompts.
            password = None if self._password_asked else self.ui.ask_password()
            self._password_asked = True
            if not password:
                hint = (
                    ""
                    if self.options.interactive
                    else " Pipe one in: printf '%s\\n' \"$PASSWORD\" | bench-setup services"
                    " --non-interactive"
                )
                raise _Failed(f"needs a password and didn't get one.{hint}")
            self._password = password
        return self._password

    def _run_cli(self, service: Service, args: list[str], stdin: str | None = None) -> str:
        code, out, err = self.host.run([service.name, *args], stdin=stdin)
        if code != 0:
            # The services' own error text names no secret (blank password,
            # mismatch, unwritable file), so it's safe to pass on.
            detail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
            raise _Failed(f"{service.name} {args[0]} failed: {detail}")
        return out

    def _port(self, service: Service, settings: dict[str, str]) -> int:
        """The port the unit will really use: settings.env wins over the default.

        An install from before the 8401-8403 move is pinned to 8000 there.
        """
        raw = settings.get(service.port_key, "")
        if not raw:
            # The .deb's postinst writes the port line; re-read in case the
            # commands above were the first thing to create the file.
            raw = self.host.read_settings(service.settings_path).get(service.port_key, "")
        if not raw:
            return service.default_port
        try:
            return int(raw)
        except ValueError:
            raise _Failed(f"{service.port_key} in {service.settings_path} isn't a number") from None

    def _start(self, service: Service, *, enabled: bool, changed: bool) -> None:
        host = self.host
        if not enabled and not host.systemctl("enable", service.name):
            raise _Failed("couldn't enable the service (systemctl enable)")
        active = host.unit_active(service.name)
        if active and not changed:
            return
        # A running service read its settings at start: restart to pick up the
        # new password or token. A failed earlier start needs clearing first.
        host.systemctl("reset-failed", service.name)
        verb = "restart" if active else "start"
        if not host.systemctl(verb, service.name):
            raise _Failed(f"couldn't {verb} the service. See: journalctl -u {service.name} -n 30")

    def _wait_for(self, service: Service, port: int) -> None:
        body: dict[str, Any] | None = None
        for attempt in range(HEALTH_TRIES):
            body = self.host.healthz(port)
            if body is not None:
                break
            if attempt < HEALTH_TRIES - 1:
                self.host.sleep(HEALTH_WAIT)
        if body is None:
            raise _Failed(
                f"nothing answered on 127.0.0.1:{port}. See: journalctl -u {service.name} -n 30"
            )
        app = body.get("app")
        if app != service.name:
            raise _Failed(
                f"127.0.0.1:{port} is answered by {app or 'something else'}, not {service.name}"
            )

    def _wire_benchhud(self, service: Service, port: int, token: str) -> str:
        """Write the URL and token into benchhud's config. Returns a note for the status line."""
        host = self.host
        if not host.installed("benchhud"):
            if not self._benchhud_note_shown:
                self._benchhud_note_shown = True
                self.ui.say(
                    "benchhud isn't installed on this machine, so there's no config to wire. "
                    "The services are still set up."
                )
            return ""
        if self.user is None:
            raise _Failed(
                "is running, but benchhud wasn't wired: can't tell whose benchhud to set up. "
                "Run bench-setup with sudo from your own account, or pass --user NAME."
            )
        if not host.user_exists(self.user):
            raise _Failed(f"is running, but benchhud wasn't wired: no such user {self.user!r}")

        url = f"http://127.0.0.1:{port}"
        current = host.read_toml(self._benchhud_config()).get(service.section, {})
        if (
            isinstance(current, dict)
            and current.get("base_url") == url
            and current.get("token") == token
        ):
            return ", benchhud wired"

        if host.benchhud_running(self.user):
            # benchhud saves its config when it closes and would write its old
            # copy over the change.
            raise _Failed(
                "is running, but benchhud wasn't wired: benchhud is open. "
                "Quit it and run bench-setup again."
            )
        for key, value in (("base_url", url), ("token", token)):
            code, _, err = host.run_as_user(
                self.user,
                ["benchhud", "config", "set", f"{service.section}.{key}", "-"],
                stdin=value,
            )
            if code != 0:
                detail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
                raise _Failed(f"is running, but benchhud config set failed: {detail}")
        return ", benchhud wired"

    def _benchhud_config(self) -> Path:
        if self._config_path is None:
            assert self.user is not None
            code, out, err = self.host.run_as_user(self.user, ["benchhud", "config", "path"])
            if code != 0 or not out.strip():
                detail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
                raise _Failed(f"is running, but benchhud config path failed: {detail}")
            self._config_path = Path(out.strip())
        return self._config_path


def run(
    options: Options,
    host: Host | None = None,
    ui: UI | None = None,
    make_ui: Callable[[bool], UI] = TerminalUI,
) -> int:
    """Set up every installed service. Returns a process exit code."""
    host = host or Host()
    ui = ui or make_ui(options.interactive)
    if not host.is_root():
        ui.warn("bench-setup services changes system services. Run it with sudo.")
        return EXIT_USAGE
    return _Run(host, ui, options).go()
