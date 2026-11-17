"""``bench-setup services``: turn the services on and point benchhud at them.

Sets one password on benchhud-intake, bench-parts and bench-recon, makes each
one's API token, starts them, checks the right app answers on each port, and
writes the URLs and tokens into benchhud's config.

Two routes, same steps:

- **deb** (the default): apt has installed the services as systemd units.
- **compose** (``--compose``): the services run as containers from the
  ``compose.yaml`` this repo ships. Everything goes through ``docker compose``,
  and it needs no root.

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
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bench_setup.host import Host, parse_settings

#: Exit codes. 2 matches argparse's "you ran it wrong".
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: How long a just-started service gets to answer /healthz.
HEALTH_TRIES = 30
HEALTH_WAIT = 1.0

PASSWORD_TRIES = 3

#: Where the compose file is looked for when ``--compose`` names none: the
#: current folder first, then where the bench-suite package installs it.
COMPOSE_FILES = (
    Path("compose.yaml"),
    Path("/usr/share/bench-suite/compose.yaml"),
    # Beside the package itself: where the Homebrew formula puts it.
    Path(__file__).resolve().parent.parent / "compose.yaml",
)

#: The port every service listens on inside its container, and where its
#: volume keeps settings.env.
CONTAINER_PORT = 8000
CONTAINER_SETTINGS = "/data/settings.env"

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

#: The oldest benchhud Mac app whose binary answers ``config`` on stdout. An
#: older one opens its window and waits instead.
BENCHHUD_MAC_MIN = (0, 14, 4)


def benchhud_mac_too_old(host: Host, user: str | None) -> str | None:
    """The installed benchhud Mac app's version if bench-setup can't drive it, else None."""
    if not host.is_macos() or host.installed("benchhud"):
        return None
    bundle = host.app_bundle("benchhud", user)
    if bundle is None:
        return None
    found = host.app_version(bundle)
    if found >= BENCHHUD_MAC_MIN:
        return None
    return ".".join(map(str, found)) or "an unknown version"


@dataclass(frozen=True)
class Service:
    """One bench service.

    ``name`` is the package, the command, the systemd unit and the app name on
    ``/healthz``: the three services keep those the same on purpose.
    ``compose_name`` is its service (and, for a dormant one, its profile) in
    compose.yaml.
    """

    name: str
    compose_name: str
    env_prefix: str
    default_port: int
    section: str  # benchhud's config section
    holds: str  # what's in it, for the messages
    dormant: bool = False  # installed off; the owner turns it on
    #: Its Mac app's name, and the oldest version whose binary takes the
    #: command line's commands. None: there is no Mac app (it runs in a container).
    mac_app: str | None = None
    mac_min: tuple[int, ...] = ()

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
    Service(
        "benchhud-intake",
        "intake",
        "INTAKE",
        8401,
        "intake",
        "customer records",
        mac_app="benchhud intake",
        mac_min=(0, 4, 0),
    ),
    Service(
        "bench-parts",
        "parts",
        "BENCH_PARTS",
        8402,
        "inventory",
        "parts stock",
        mac_app="bench parts",
        mac_min=(0, 5, 0),
    ),
    Service(
        "bench-recon", "recon", "BENCHRECON", 8403, "recon", "client engagement data", dormant=True
    ),
)


@dataclass(frozen=True)
class Options:
    interactive: bool = True
    #: True/False from --recon/--no-recon; None asks (or leaves it off, non-interactive).
    recon: bool | None = None
    #: The desktop user benchhud belongs to; None means whoever ran sudo.
    user: str | None = None
    #: The compose file to drive; None means the deb route.
    compose: Path | None = None


class UI(Protocol):
    def say(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def ask_password(self) -> str | None: ...
    def ask_yes_no(self, question: str) -> bool: ...
    def ask_choice(self, question: str, count: int) -> int | None: ...


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
        try:
            for _ in range(PASSWORD_TRIES):
                first = getpass.getpass("Password for the bench services: ")
                if not first.strip():
                    self.warn("The password can't be blank.")
                    continue
                if first != getpass.getpass("Again: "):
                    self.warn("The passwords didn't match.")
                    continue
                return first
        except EOFError:
            pass  # no one at the keyboard: the same as giving no password
        return None

    def ask_yes_no(self, question: str) -> bool:
        if not self.interactive:
            return False
        try:
            return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:
            print()
            return False  # no one at the keyboard is a no

    def ask_choice(self, question: str, count: int) -> int | None:
        """A number from 1 to ``count``, as an index; None for Enter or anything else."""
        if not self.interactive:
            return None
        try:
            answer = input(f"{question} [1-{count}, Enter for none] ").strip()
        except EOFError:
            print()
            return None
        return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= count else None


class _Failed(Exception):
    """One service couldn't be set up. The message is safe to print."""


def _detail(code: int, err: str) -> str:
    """The last line of a command's stderr, or its exit code."""
    return err.strip().splitlines()[-1] if err.strip() else f"exit {code}"


class _Systemd:
    """The deb route: each service is a systemd unit with a root-only settings.env."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def missing(self, service: Service) -> str | None:
        if self.host.installed(service.name):
            return None
        return f"not installed (sudo apt install {service.name})"

    def none_hint(self) -> str:
        return "None of the bench services are installed. Try: sudo apt install bench-suite"

    def logs(self, service: Service) -> str:
        return f"journalctl -u {service.name} -n 30"

    def enabled(self, service: Service) -> bool:
        return self.host.unit_enabled(service.name)

    def settings(self, service: Service) -> dict[str, str]:
        return self.host.read_settings(service.settings_path)

    def cli(self, service: Service, args: list[str], stdin: str | None) -> tuple[int, str, str]:
        return self.host.run([service.name, *args], stdin=stdin)

    def port(self, service: Service, settings: dict[str, str]) -> int:
        """The port the unit will really use: settings.env wins over the default.

        An install from before the 8401-8403 move is pinned to 8000 there.
        """
        raw = settings.get(service.port_key, "")
        if not raw:
            # The .deb's postinst writes the port line; re-read in case the
            # commands above were the first thing to create the file.
            raw = self.settings(service).get(service.port_key, "")
        if not raw:
            return service.default_port
        try:
            return int(raw)
        except ValueError:
            raise _Failed(f"{service.port_key} in {service.settings_path} isn't a number") from None

    def start(self, service: Service, *, enabled: bool, changed: bool) -> None:
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
            raise _Failed(f"couldn't {verb} the service. See: {self.logs(service)}")


class _MacApps:
    """macOS: intake and parts are menu-bar apps, set up through their own binaries.

    Each app's binary takes the service's commands (``set-password --stdin``,
    ``new-token --print``, ``paths``) and keeps its settings in the user's own
    Application Support folder, so nothing here needs root. The app reads its
    settings when it starts: after a change, a running app is quit and reopened.
    """

    ASK_TRIES = 6
    QUIT_TRIES = 20
    QUIT_WAIT = 0.5

    def __init__(self, host: Host, user: str | None) -> None:
        self.host = host
        self.user = user

    def _bundle(self, service: Service) -> Path | None:
        return self.host.app_bundle(service.mac_app, self.user) if service.mac_app else None

    def _binary(self, service: Service) -> str:
        bundle = self._bundle(service)
        assert bundle is not None and service.mac_app is not None
        return str(bundle / "Contents" / "MacOS" / service.mac_app)

    def _as_user(self, cmd: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if self.user is None:
            return self.host.run(cmd, stdin=stdin)
        return self.host.run_as_user(self.user, cmd, stdin=stdin)

    def _gone(self, app: str, tries: int) -> bool:
        for _ in range(tries):
            if not self.host.process_running(app):
                return True
            self.host.sleep(self.QUIT_WAIT)
        return not self.host.process_running(app)

    def missing(self, service: Service) -> str | None:
        if service.mac_app is None:
            return (
                "has no Mac app. It runs in a container: "
                f"bench-setup services --compose{' --recon' if service.dormant else ''}"
            )
        bundle = self._bundle(service)
        if bundle is None:
            return f"the {service.mac_app} app isn't in Applications"
        found = self.host.app_version(bundle)
        if found < service.mac_min:
            # An older app would open its menu-bar icon and wait, not answer.
            have = ".".join(map(str, found)) or "an unknown version"
            need = ".".join(map(str, service.mac_min))
            return f"the {service.mac_app} app is {have}; setting it up from here needs {need}"
        return None

    def none_hint(self) -> str:
        return (
            "None of the bench apps are in Applications. Install them, or run the "
            "services in containers: bench-setup services --compose"
        )

    def logs(self, service: Service) -> str:
        return f"the {service.mac_app} icon in the menu bar"

    def enabled(self, service: Service) -> bool:
        return True

    def settings(self, service: Service) -> dict[str, str]:
        code, out, err = self.cli(service, ["paths"], None)
        if code != 0:
            raise _Failed(f"{service.name} paths failed: {_detail(code, err)}")
        for line in out.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "settings" and value.strip():
                return self.host.read_settings(Path(value.strip()))
        raise _Failed(f"the {service.mac_app} app didn't say where its settings are")

    def cli(self, service: Service, args: list[str], stdin: str | None) -> tuple[int, str, str]:
        return self._as_user([self._binary(service), *args], stdin)

    def port(self, service: Service, settings: dict[str, str]) -> int:
        raw = settings.get(service.port_key, "")
        return int(raw) if raw.isdigit() else service.default_port

    def start(self, service: Service, *, enabled: bool, changed: bool) -> None:
        host = self.host
        app = service.mac_app
        assert app is not None
        if host.process_running(app):
            if not changed:
                return
            # Ask nicely first. A menu-bar app can decline an Apple event quit
            # (the real ones answer "User canceled"), so then it's told to stop.
            self._as_user(["osascript", "-e", f'tell application "{app}" to quit'])
            if not self._gone(app, self.ASK_TRIES):
                self._as_user(["pkill", "-TERM", "-x", app])
                if not self._gone(app, self.QUIT_TRIES):
                    raise _Failed(
                        f"the {app} app didn't quit, so it's still running with its old "
                        "settings. Quit it from the menu bar and run bench-setup again."
                    )
        # -g: leave whatever the owner is doing in front.
        code, _, err = self._as_user(["open", "-g", str(self._bundle(service))])
        if code != 0:
            raise _Failed(f"couldn't open the {app} app: {_detail(code, err)}")


class _Compose:
    """The container route: everything goes through ``docker compose -f FILE``.

    settings.env lives in each service's volume, so it's read and written by
    running the service's own image against that volume.
    """

    def __init__(self, host: Host, file: Path) -> None:
        self.host = host
        self.file = file
        # A dormant service sits behind a profile of its own name. Naming the
        # profile lets every command see it; nothing starts unless asked for.
        profiles = [arg for s in SERVICES if s.dormant for arg in ("--profile", s.compose_name)]
        self._base = ["docker", "compose", "-f", str(file), *profiles]
        self._config: dict[str, Any] | None = None

    def _dc(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        return self.host.run([*self._base, *args], stdin=stdin)

    def _run_in(self, service: Service, cmd: list[str], stdin: str | None = None):
        # A one-off container on the service's volume. -T: no terminal, so
        # stdin and stdout are plain pipes.
        return self._dc("run", "--rm", "--no-deps", "-T", service.compose_name, *cmd, stdin=stdin)

    def _services(self) -> dict[str, Any]:
        if self._config is None:
            code, out, err = self._dc("config", "--format", "json")
            if code != 0:
                raise _Failed(f"docker compose can't read {self.file}: {_detail(code, err)}")
            try:
                config = json.loads(out)
            except ValueError:
                raise _Failed(f"docker compose config gave no JSON for {self.file}") from None
            self._config = config if isinstance(config, dict) else {}
        found = self._config.get("services")
        return found if isinstance(found, dict) else {}

    def missing(self, service: Service) -> str | None:
        if service.compose_name in self._services():
            return None
        return f"no {service.compose_name!r} service in {self.file}"

    def none_hint(self) -> str:
        return f"None of the bench services are in {self.file}."

    def logs(self, service: Service) -> str:
        return f"docker compose -f {self.file} logs {service.compose_name}"

    def enabled(self, service: Service) -> bool:
        """A container for it exists, running or not: someone has brought it up."""
        code, out, _ = self._dc("ps", "--all", "--quiet", service.compose_name)
        return code == 0 and bool(out.strip())

    def settings(self, service: Service) -> dict[str, str]:
        code, out, err = self._run_in(
            service, ["sh", "-c", f"cat {CONTAINER_SETTINGS} 2>/dev/null || true"]
        )
        if code != 0:
            raise _Failed(f"couldn't read its settings: {_detail(code, err)}")
        return parse_settings(out)

    def cli(self, service: Service, args: list[str], stdin: str | None) -> tuple[int, str, str]:
        return self._run_in(service, [service.name, *args], stdin=stdin)

    def port(self, service: Service, settings: dict[str, str]) -> int:
        """The host port compose.yaml publishes the service on."""
        ports = self._services()[service.compose_name].get("ports") or []
        for entry in ports:
            if not isinstance(entry, dict) or entry.get("target") != CONTAINER_PORT:
                continue
            host_ip = str(entry.get("host_ip") or "")
            if service.dormant and host_ip not in _LOOPBACK:
                # It holds client data: bench-setup runs it on this machine only.
                raise _Failed(
                    f"{self.file} publishes it beyond this machine. "
                    f'Use "127.0.0.1:{service.default_port}:{CONTAINER_PORT}".'
                )
            try:
                return int(entry.get("published"))
            except (TypeError, ValueError):
                break
        raise _Failed(f"{self.file} doesn't publish its port {CONTAINER_PORT}")

    def start(self, service: Service, *, enabled: bool, changed: bool) -> None:
        name = service.compose_name
        code, out, _ = self._dc("ps", "--status", "running", "--quiet", name)
        running = code == 0 and bool(out.strip())
        if running and not changed:
            return
        # A running container read its settings at start: restart to pick up
        # the new password or token.
        args = ("restart", name) if running else ("up", "-d", "--no-deps", name)
        code, _, err = self._dc(*args)
        if code != 0:
            raise _Failed(f"couldn't start it: {_detail(code, err)}. See: {self.logs(service)}")


class _Run:
    """One pass over the services."""

    def __init__(self, host: Host, ui: UI, options: Options) -> None:
        self.host = host
        self.ui = ui
        self.options = options
        # Whose benchhud: --user, else whoever ran sudo, else (no sudo, which
        # only the compose route allows) whoever is running this.
        self.user = (
            options.user or host.sudo_user() or (None if host.is_root() else host.current_user())
        )
        self.backend: _Systemd | _Compose | _MacApps
        if options.compose:
            self.backend = _Compose(host, options.compose)
        elif host.is_macos():
            self.backend = _MacApps(host, self.user)
        else:
            self.backend = _Systemd(host)
        self._password: str | None = None
        self._password_asked = False
        self._config_path: Path | None = None
        self._benchhud_note_shown = False

    # -- the pass ---------------------------------------------------------

    def go(self) -> int:
        failed = False
        seen = False
        for service in SERVICES:
            try:
                missing = self.backend.missing(service)
                if missing:
                    self.ui.say(f"{service.name}: {missing}")
                    continue
                seen = True
                self._set_up(service)
            except _Failed as exc:
                failed = True
                self.ui.warn(f"{service.name}: {exc}")
        if failed:
            return EXIT_FAILED
        if not seen:
            self.ui.warn(self.backend.none_hint())
            return EXIT_FAILED
        return EXIT_FAILED if failed else EXIT_OK

    def _set_up(self, service: Service) -> None:
        backend = self.backend
        enabled = backend.enabled(service)
        if service.dormant and not enabled and not self._turn_on(service):
            self.ui.say(f"{service.name}: installed, off")
            return

        settings = backend.settings(service)
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

        port = backend.port(service, settings)
        backend.start(service, enabled=enabled, changed=changed)
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
                    f" --non-interactive{' --compose' if self.options.compose else ''}"
                )
                raise _Failed(f"needs a password and didn't get one.{hint}")
            self._password = password
        return self._password

    def _run_cli(self, service: Service, args: list[str], stdin: str | None = None) -> str:
        code, out, err = self.backend.cli(service, args, stdin)
        if code != 0:
            # The services' own error text names no secret (blank password,
            # mismatch, unwritable file), so it's safe to pass on.
            raise _Failed(f"{service.name} {args[0]} failed: {_detail(code, err)}")
        return out

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
                f"nothing answered on 127.0.0.1:{port}. See: {self.backend.logs(service)}"
            )
        app = body.get("app")
        if app != service.name:
            raise _Failed(
                f"127.0.0.1:{port} is answered by {app or 'something else'}, not {service.name}"
            )

    def _wire_benchhud(self, service: Service, port: int, token: str) -> str:
        """Write the URL and token into benchhud's config. Returns a note for the status line."""
        host = self.host
        old = benchhud_mac_too_old(host, self.user)
        if old is not None:
            need = ".".join(map(str, BENCHHUD_MAC_MIN))
            raise _Failed(
                f"is running, but benchhud wasn't wired: the benchhud app is {old}, and "
                f"setting it up from here needs {need}. Update it and run bench-setup again."
            )
        benchhud = host.benchhud(self.user)
        if benchhud is None:
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
                [benchhud, "config", "set", f"{service.section}.{key}", "-"],
                stdin=value,
            )
            if code != 0:
                raise _Failed(f"is running, but benchhud config set failed: {_detail(code, err)}")
        return ", benchhud wired"

    def _benchhud_config(self) -> Path:
        if self._config_path is None:
            assert self.user is not None
            benchhud = self.host.benchhud(self.user) or "benchhud"
            code, out, err = self.host.run_as_user(self.user, [benchhud, "config", "path"])
            if code != 0 or not out.strip():
                raise _Failed(f"is running, but benchhud config path failed: {_detail(code, err)}")
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
    if options.compose is None and host.is_macos():
        # The Mac apps belong to the person at the keyboard: no sudo, and a
        # bare root shell has no one's apps to set up.
        if host.is_root() and not (options.user or host.sudo_user()):
            ui.warn("On a Mac, run bench-setup services as yourself, without sudo.")
            return EXIT_USAGE
    elif options.compose is None:
        if not host.is_root():
            ui.warn("bench-setup services changes system services. Run it with sudo.")
            return EXIT_USAGE
    else:
        if not host.is_file(options.compose):
            ui.warn(f"No compose file at {options.compose}.")
            return EXIT_USAGE
        if not host.installed("docker") or host.run(["docker", "compose", "version"])[0] != 0:
            ui.warn("The compose route needs Docker with the compose plugin (docker compose).")
            return EXIT_FAILED
        ui.say(
            f"Using {options.compose}. The first run downloads the images, "
            "which can take a few minutes."
        )
    return _Run(host, ui, options).go()


def find_compose_file(host: Host | None = None) -> Path | None:
    """The compose file ``--compose`` means when it names none."""
    host = host or Host()
    for candidate in COMPOSE_FILES:
        if host.is_file(candidate):
            return candidate.resolve()
    return None
