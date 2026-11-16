"""``bench-setup services --compose`` against a fake Docker (bench-suite#6).

The fake models what ``docker compose -f compose.yaml`` does with the file this
repo ships: one-off ``run`` containers that read and write settings.env in each
service's volume, and ``up`` / ``restart`` / ``ps`` for the long-running ones.
The real thing runs in CI (the ``compose-e2e`` job).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench_setup import services
from bench_setup.cli import main
from bench_setup.services import EXIT_FAILED, EXIT_OK, EXIT_USAGE, SERVICES
from test_services import CONFIG, PASSWORD, FakeHost, FakeUI, setup

FILE = Path("/srv/bench/compose.yaml")
BY_COMPOSE = {s.compose_name: s for s in SERVICES}


class ComposeHost(FakeHost):
    def __init__(self) -> None:
        super().__init__()
        # No sudo and no packages: just Docker, benchhud and the compose file.
        self.root = False
        self.sudo = None
        self.me = "aaron"
        self.commands = {"docker", "benchhud"}
        self.files = {FILE}
        self.settings = {s.name: {} for s in SERVICES}
        self.containers: dict[str, str] = {}  # compose name -> "running" | "exited"
        self.compose_ok = True
        self.ports: dict[str, dict[str, Any]] = {
            s.compose_name: {
                "host_ip": "127.0.0.1",
                "target": 8000,
                "published": str(s.default_port),
            }
            for s in SERVICES
        }

    def current_user(self) -> str:
        return self.me

    def is_file(self, path: Path) -> bool:
        return path in self.files

    def run(self, cmd, stdin=None):
        cmd = tuple(cmd)
        if cmd[:2] != ("docker", "compose"):
            return super().run(cmd, stdin)
        self.calls.append(("me", cmd, stdin))
        if cmd[2:] == ("version",):
            return (0 if self.compose_ok else 1), "", ""
        assert cmd[2:4] == ("-f", str(FILE)), "every command names the file"
        assert cmd[4:6] == ("--profile", "recon"), "recon's profile is always visible"
        verb, *args = cmd[6:]
        return getattr(self, f"_dc_{verb}")(args, stdin)

    def _dc_config(self, args, stdin):
        body = {"services": {name: {"ports": [port]} for name, port in self.ports.items()}}
        return 0, json.dumps(body), ""

    def _dc_ps(self, args, stdin):
        name = args[-1]
        state = self.containers.get(name)
        wanted = state is not None if "--all" in args else state == "running"
        return 0, ("abc123\n" if wanted else ""), ""

    def _dc_run(self, args, stdin):
        assert args[:3] == ["--rm", "--no-deps", "-T"]
        service = BY_COMPOSE[args[3]]
        inner = args[4:]
        if inner[0] == "sh":
            lines = [f"{k}={v}" for k, v in self.settings[service.name].items()]
            return 0, "\n".join(lines) + "\n", " Image pulled\n"
        assert inner[0] == service.name
        return super().run(inner, stdin)

    def _dc_up(self, args, stdin):
        assert args[:2] == ["-d", "--no-deps"]
        return self._boot(args[2])

    def _dc_restart(self, args, stdin):
        return self._boot(args[0])

    def _boot(self, name: str):
        service = BY_COMPOSE[name]
        # Like the real images: no password, the container exits.
        has_password = bool(self.settings[service.name].get(service.password_key))
        self.containers[name] = "running" if has_password else "exited"
        return 0, "", ""

    def healthz(self, port: int) -> dict[str, Any] | None:
        for name, state in self.containers.items():
            if state == "running" and int(self.ports[name]["published"]) == port:
                return {"status": "ok", "app": BY_COMPOSE[name].name, "version": "1"}
        return None

    def run_as_user(self, user, cmd, stdin=None):
        assert user == self.me, "without root it can only be the current user"
        return super().run_as_user(user, cmd, stdin)


def compose(host: ComposeHost, ui: FakeUI | None = None, **options: Any) -> tuple[int, FakeUI]:
    return setup(host, ui, compose=FILE, **options)


def test_fresh_compose_sets_up_intake_and_parts_without_root() -> None:
    host = ComposeHost()
    code, ui = compose(host)

    assert code == EXIT_OK, ui.text
    assert ui.password_asks == 1
    assert host.containers == {"intake": "running", "parts": "running"}
    assert host.config["intake"] == {
        "base_url": "http://127.0.0.1:8401",
        "token": host.settings["benchhud-intake"]["INTAKE_API_TOKEN"],
    }
    assert host.config["inventory"]["base_url"] == "http://127.0.0.1:8402"
    assert not host.ran("systemctl"), "the compose route never touches systemd"


def test_compose_recon_stays_off_unless_asked() -> None:
    host = ComposeHost()
    code, ui = compose(host)

    assert code == EXIT_OK
    assert "recon" not in host.containers
    assert "recon" not in host.config
    assert "bench-recon: installed, off" in ui.out
    assert not [c for c in host.ran("docker", "compose") if c[6:7] == ("run",) and "recon" in c[7:]]


def test_compose_recon_flag_brings_it_up_on_loopback() -> None:
    host = ComposeHost()
    code, ui = compose(host, recon=True)

    assert code == EXIT_OK, ui.text
    assert host.containers["recon"] == "running"
    assert host.config["recon"]["base_url"] == "http://127.0.0.1:8403"


def test_compose_refuses_recon_published_beyond_this_machine() -> None:
    host = ComposeHost()
    host.ports["recon"]["host_ip"] = ""  # "8403:8000": every interface
    code, ui = compose(host, recon=True)

    assert code == EXIT_FAILED
    assert "recon" not in host.containers, "never started"
    assert "recon" not in host.config
    assert any("beyond this machine" in line for line in ui.err)
    assert host.containers["intake"] == "running", "the others still get set up"


def test_compose_rerun_changes_nothing() -> None:
    host = ComposeHost()
    compose(host, recon=True)
    tokens = {name: dict(values) for name, values in host.settings.items()}
    host.calls.clear()

    ui = FakeUI()
    code, ui = compose(host, ui)

    assert code == EXIT_OK
    assert ui.password_asks == 0
    assert host.settings == tokens, "passwords and tokens are kept"
    verbs = {c[6] for c in host.ran("docker", "compose") if len(c) > 6}
    assert verbs <= {"config", "ps", "run"}, "no up, no restart"
    assert not [c for c in host.ran("docker", "compose") if "set-password" in c or "new-token" in c]
    assert not host.ran("benchhud", "config", "set")
    assert sum("already set up" in line for line in ui.out) == 3


def test_compose_follows_the_port_in_the_file() -> None:
    host = ComposeHost()
    host.ports["intake"]["published"] = "9401"
    code, _ = compose(host)

    assert code == EXIT_OK
    assert host.config["intake"]["base_url"] == "http://127.0.0.1:9401"


def test_compose_secrets_never_ride_in_argv_or_output() -> None:
    host = ComposeHost()
    code, ui = compose(host, recon=True)

    assert code == EXIT_OK
    secrets = [PASSWORD] + [
        values[s.token_key] for s in SERVICES for values in [host.settings[s.name]]
    ]
    for _, cmd, _ in host.calls:
        for secret in secrets:
            assert all(secret not in arg for arg in cmd)
    for secret in secrets:
        assert secret not in ui.text


def test_compose_restarts_a_running_container_that_got_a_new_token() -> None:
    host = ComposeHost()
    compose(host)
    del host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    host.calls.clear()

    code, _ = compose(host)

    assert code == EXIT_OK
    assert [c[6:] for c in host.ran("docker", "compose") if c[6:7] == ("restart",)] == [
        ("restart", "parts")
    ]
    assert (
        host.config["inventory"]["token"] == host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    )


def test_compose_service_missing_from_the_file_is_reported_not_fatal() -> None:
    host = ComposeHost()
    del host.ports["parts"]
    code, ui = compose(host)

    assert code == EXIT_OK
    assert any("bench-parts: no 'parts' service" in line for line in ui.out)
    assert host.containers == {"intake": "running"}


def test_compose_needs_the_file_and_docker() -> None:
    host = ComposeHost()
    host.files.clear()
    code, ui = compose(host)
    assert code == EXIT_USAGE
    assert "No compose file" in ui.text

    host = ComposeHost()
    host.compose_ok = False
    code, ui = compose(host)
    assert code == EXIT_FAILED
    assert "docker compose" in ui.text
    assert not host.ran("docker", "compose", "-f")


def test_compose_under_sudo_wires_the_sudo_users_benchhud() -> None:
    host = ComposeHost()
    host.root, host.sudo, host.me = True, "aaron", "root"
    host.run_as_user = lambda user, cmd, stdin=None: FakeHost.run_as_user(host, user, cmd, stdin)
    code, _ = compose(host)

    assert code == EXIT_OK
    assert {who for who, cmd, _ in host.calls if cmd[0] == "benchhud"} == {"aaron"}


def test_deb_route_still_needs_root() -> None:
    host = ComposeHost()
    code, _ = setup(host)
    assert code == EXIT_USAGE


def test_cli_compose_flag_finds_the_file(monkeypatch, tmp_path, capsys) -> None:
    seen: list[services.Options] = []
    monkeypatch.setattr(services, "run", lambda options: seen.append(options) or 0)

    monkeypatch.chdir(tmp_path)
    assert main(["services", "--compose"]) == EXIT_USAGE
    assert "no compose file" in capsys.readouterr().err

    (tmp_path / "compose.yaml").write_text("services: {}\n")
    assert main(["services", "--compose"]) == 0
    assert seen[-1].compose == (tmp_path / "compose.yaml").resolve()

    other = tmp_path / "other.yaml"
    assert main(["services", "--compose", str(other), "--recon"]) == 0
    assert seen[-1].compose == other.resolve() and seen[-1].recon is True

    assert main(["services"]) == 0
    assert seen[-1].compose is None


def test_shipped_compose_file_matches_the_services() -> None:
    """The compose.yaml in this repo: right ports, loopback only, recon behind its profile."""
    text = (Path(__file__).parent.parent / "compose.yaml").read_text()
    for service in SERVICES:
        assert f"\n  {service.compose_name}:\n" in text
        assert f'- "127.0.0.1:{service.default_port}:8000"' in text
        assert f"ghcr.io/iammrcupp/{service.name}:" in text
    assert ":latest" not in text
    assert 'profiles: ["recon"]' in text
    assert text.count("profiles:") == 1
    assert CONFIG  # shared fixture import stays used
