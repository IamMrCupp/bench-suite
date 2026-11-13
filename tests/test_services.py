"""``bench-setup services`` against a fake machine (bench-suite#3).

The fake models what the three .debs install: each service's settings.env, its
unit's enabled/active state, what answers on its port, and benchhud's config.
Every command bench-setup runs is recorded, so the tests can check what it did
and that no secret ever rode in argv or reached the output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bench_setup import services
from bench_setup.cli import main
from bench_setup.services import EXIT_FAILED, EXIT_OK, EXIT_USAGE, SERVICES, Options

PASSWORD = "correct horse battery"
CONFIG = Path("/home/aaron/.config/benchhud/config.toml")
BY_NAME = {s.name: s for s in SERVICES}


class FakeHost:
    def __init__(self) -> None:
        self.root = True
        self.sudo = "aaron"
        self.users = {"aaron"}
        self.commands = {"benchhud-intake", "bench-parts", "bench-recon", "benchhud"}
        # What the .debs leave behind: intake and parts enabled (not started,
        # no password yet) with their port recorded; recon dormant, no file.
        self.settings: dict[str, dict[str, str]] = {
            "benchhud-intake": {"INTAKE_PORT": "8401"},
            "bench-parts": {"BENCH_PARTS_PORT": "8402"},
            "bench-recon": {},
        }
        self.enabled = {"benchhud-intake", "bench-parts"}
        self.active: set[str] = set()
        self.config: dict[str, dict[str, str]] = {}
        self.benchhud_open = False
        self.answering: dict[int, str] = {}  # port -> app, overrides the units
        self.silent: set[int] = set()  # ports where nothing ever answers
        self.slept = 0
        self.calls: list[tuple[str, tuple[str, ...], str | None]] = []
        self.tokens_made = 0

    # -- what bench-setup asks of the machine --

    def is_root(self) -> bool:
        return self.root

    def sudo_user(self) -> str | None:
        return self.sudo

    def user_exists(self, user: str) -> bool:
        return user in self.users

    def installed(self, command: str) -> bool:
        return command in self.commands

    def read_settings(self, path: Path) -> dict[str, str]:
        return dict(self.settings[path.parent.name])

    def run(self, cmd, stdin=None):
        cmd = tuple(cmd)
        self.calls.append(("root", cmd, stdin))
        name, *args = cmd
        if name == "systemctl":
            return self._systemctl(args)
        if name == "pgrep":
            return (0 if self.benchhud_open else 1), "", ""
        service = BY_NAME[name]
        if args == ["set-password", "--stdin"]:
            if not (stdin or "").strip():
                return 1, "", "The password can't be blank.\n"
            self.settings[name][service.password_key] = "scrypt$hash"
            return 0, "", "Saved.\n"
        if args == ["new-token", "--print"]:
            self.tokens_made += 1
            token = f"tok-{name}-{self.tokens_made}"
            self.settings[name][service.token_key] = token
            return 0, token + "\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    def _systemctl(self, args):
        verb, *rest = args
        unit = rest[-1]
        if verb == "is-enabled":
            return (0 if unit in self.enabled else 1), "", ""
        if verb == "is-active":
            return (0 if unit in self.active else 1), "", ""
        if verb == "enable":
            self.enabled.add(unit)
        elif verb in {"start", "restart"}:
            # Like the real units: no password, no service (exit 2).
            if not self.settings[unit].get(BY_NAME[unit].password_key):
                return 1, "", "failed"
            self.active.add(unit)
        return 0, "", ""

    def run_as_user(self, user, cmd, stdin=None):
        cmd = tuple(cmd)
        self.calls.append((user, cmd, stdin))
        assert cmd[:2] == ("benchhud", "config")
        if cmd[2] == "path":
            return 0, f"{CONFIG}\n", ""
        assert cmd[2] == "set" and cmd[4] == "-", "values must come in on stdin"
        section, key = cmd[3].split(".")
        self.config.setdefault(section, {})[key] = stdin or ""
        return 0, f"set {cmd[3]} in {CONFIG}\n", ""

    def unit_enabled(self, unit: str) -> bool:
        return unit in self.enabled

    def unit_active(self, unit: str) -> bool:
        return unit in self.active

    def systemctl(self, *args: str) -> bool:
        return self.run(["systemctl", *args])[0] == 0

    def healthz(self, port: int) -> dict[str, Any] | None:
        if port in self.silent:
            return None
        if port in self.answering:
            return {"status": "ok", "app": self.answering[port], "version": "1"}
        for name in self.active:
            service = BY_NAME[name]
            if int(self.settings[name].get(service.port_key, service.default_port)) == port:
                return {"status": "ok", "app": name, "version": "1"}
        return None

    def benchhud_running(self, user: str) -> bool:
        return self.run(["pgrep", "-u", user, "-x", "benchhud"])[0] == 0

    def read_toml(self, path: Path) -> dict[str, Any]:
        assert path == CONFIG
        return {k: dict(v) for k, v in self.config.items()}

    def sleep(self, seconds: float) -> None:
        self.slept += 1

    # -- helpers for the tests --

    def ran(self, *fragment: str) -> list[tuple[str, ...]]:
        return [c for _, c, _ in self.calls if c[: len(fragment)] == fragment]

    def changes(self) -> list[tuple[str, ...]]:
        """Commands that change the machine (not the is-enabled / pgrep / path reads)."""
        reads = {"is-enabled", "is-active"}
        return [
            c
            for _, c, _ in self.calls
            if not (c[0] == "systemctl" and c[1] in reads)
            and c[0] != "pgrep"
            and c[:3] != ("benchhud", "config", "path")
        ]


class FakeUI:
    def __init__(self, password: str | None = PASSWORD, yes: bool = False) -> None:
        self.password = password
        self.yes = yes
        self.out: list[str] = []
        self.err: list[str] = []
        self.password_asks = 0
        self.questions: list[str] = []

    def say(self, message: str) -> None:
        self.out.append(message)

    def warn(self, message: str) -> None:
        self.err.append(message)

    def ask_password(self) -> str | None:
        self.password_asks += 1
        return self.password

    def ask_yes_no(self, question: str) -> bool:
        self.questions.append(question)
        return self.yes

    @property
    def text(self) -> str:
        return "\n".join(self.out + self.err)


def setup(host: FakeHost, ui: FakeUI | None = None, **options: Any) -> tuple[int, FakeUI]:
    ui = ui or FakeUI()
    return services.run(Options(**options), host=host, ui=ui), ui  # type: ignore[arg-type]


# -- a fresh install ------------------------------------------------------


def test_fresh_install_sets_up_intake_and_parts_and_wires_benchhud() -> None:
    host = FakeHost()
    code, ui = setup(host)

    assert code == EXIT_OK
    assert ui.password_asks == 1, "one shared prompt"
    for name in ("benchhud-intake", "bench-parts"):
        assert host.settings[name][BY_NAME[name].password_key]
        assert name in host.active and name in host.enabled
    assert host.config["intake"] == {
        "base_url": "http://127.0.0.1:8401",
        "token": host.settings["benchhud-intake"]["INTAKE_API_TOKEN"],
    }
    assert host.config["inventory"] == {
        "base_url": "http://127.0.0.1:8402",
        "token": host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"],
    }


def test_benchhud_config_is_written_as_the_desktop_user_not_root() -> None:
    host = FakeHost()
    setup(host)
    writers = {who for who, cmd, _ in host.calls if cmd[:3] == ("benchhud", "config", "set")}
    assert writers == {"aaron"}


def test_recon_is_left_off_by_default() -> None:
    host = FakeHost()
    code, ui = setup(host)

    assert code == EXIT_OK
    assert "bench-recon: installed, off" in ui.out
    assert len(ui.questions) == 1 and "client engagement data" in ui.questions[0]
    assert not host.ran("bench-recon")
    assert "bench-recon" not in host.enabled | host.active
    assert host.settings["bench-recon"] == {}
    assert "recon" not in host.config


# -- running it again -----------------------------------------------------


def test_a_second_run_changes_nothing_and_asks_for_no_password() -> None:
    host = FakeHost()
    setup(host)
    before = (dict(host.config), {k: dict(v) for k, v in host.settings.items()})
    host.calls.clear()

    code, ui = setup(host)

    assert code == EXIT_OK
    assert ui.password_asks == 0
    assert host.changes() == []
    assert (host.config, host.settings) == before
    assert any("already set up" in line for line in ui.out)


def test_an_existing_token_is_reused_not_rotated() -> None:
    """Something else may be using it; a new one would cut that off."""
    host = FakeHost()
    host.settings["bench-parts"].update(
        BENCH_PARTS_PASSWORD_HASH="scrypt$old", BENCH_PARTS_API_TOKEN="already-there"
    )
    host.active.add("bench-parts")

    code, _ = setup(host)

    assert code == EXIT_OK
    assert not host.ran("bench-parts", "new-token")
    assert not host.ran("bench-parts", "set-password")
    assert not host.ran("systemctl", "restart", "bench-parts"), "nothing changed, no restart"
    assert host.config["inventory"]["token"] == "already-there"


def test_a_running_service_is_restarted_when_its_settings_change() -> None:
    host = FakeHost()
    host.settings["bench-parts"]["BENCH_PARTS_PASSWORD_HASH"] = "scrypt$old"
    host.active.add("bench-parts")  # running with a password, but no token yet

    setup(host)

    assert host.ran("systemctl", "restart", "bench-parts")


# -- secrets --------------------------------------------------------------


def test_no_secret_is_ever_in_argv_or_in_the_output() -> None:
    host = FakeHost()
    code, ui = setup(host, recon=True)
    assert code == EXIT_OK

    secrets = [PASSWORD] + [host.settings[s.name][s.token_key] for s in SERVICES]
    assert len(secrets) == 4
    argv = " ".join(" ".join(cmd) for _, cmd, _ in host.calls)
    for secret in secrets:
        assert secret not in argv
        assert secret not in ui.text


# -- bench-recon ----------------------------------------------------------


@pytest.mark.parametrize("how", ["flag", "answer"])
def test_recon_turned_on_is_set_up_like_the_others(how: str) -> None:
    host = FakeHost()
    ui = FakeUI(yes=(how == "answer"))
    code, ui = setup(host, ui, recon=True if how == "flag" else None)

    assert code == EXIT_OK
    assert ui.password_asks == 1, "the same prompt covers recon in this run"
    assert "bench-recon" in host.enabled and "bench-recon" in host.active
    assert host.config["recon"] == {
        "base_url": "http://127.0.0.1:8403",
        "token": host.settings["bench-recon"]["BENCHRECON_API_TOKEN"],
    }


def test_no_recon_flag_skips_the_question() -> None:
    host = FakeHost()
    _, ui = setup(host, recon=False)
    assert ui.questions == []
    assert not host.ran("bench-recon")


def test_non_interactive_never_turns_recon_on_by_itself() -> None:
    host = FakeHost()
    ui = FakeUI(yes=True)  # would say yes if asked
    setup(host, ui, interactive=False)
    assert ui.questions == []
    assert "bench-recon" not in host.enabled


def test_recon_already_on_is_kept_wired_without_asking() -> None:
    host = FakeHost()
    setup(host, recon=True)
    host.calls.clear()

    code, ui = setup(host)  # no flag this time

    assert code == EXIT_OK
    assert ui.questions == []
    assert host.changes() == []


# -- ports ----------------------------------------------------------------


def test_the_port_comes_from_settings_env_not_the_default() -> None:
    """An install from before the 8401 move is pinned to 8000."""
    host = FakeHost()
    host.settings["benchhud-intake"]["INTAKE_PORT"] = "8000"

    code, _ = setup(host)

    assert code == EXIT_OK
    assert host.config["intake"]["base_url"] == "http://127.0.0.1:8000"


def test_something_else_on_the_port_is_a_failure_and_isnt_wired() -> None:
    host = FakeHost()
    host.answering[8402] = "grafana"

    code, ui = setup(host)

    assert code == EXIT_FAILED
    assert any("bench-parts" in line and "grafana" in line for line in ui.err)
    assert "inventory" not in host.config
    assert "intake" in host.config, "the other service still gets set up"


def test_an_answer_that_names_no_app_is_a_failure() -> None:
    host = FakeHost()
    host.answering[8401] = ""
    code, _ = setup(host)
    assert code == EXIT_FAILED
    assert "intake" not in host.config


def test_a_service_that_never_answers_is_a_failure_that_points_at_the_journal() -> None:
    host = FakeHost()
    host.silent.add(8401)

    code, ui = setup(host)

    assert code == EXIT_FAILED
    assert any("journalctl -u benchhud-intake" in line for line in ui.err)
    assert "intake" not in host.config
    assert host.slept == services.HEALTH_TRIES - 1, "it waits between tries, not after the last"


# -- benchhud -------------------------------------------------------------


def test_an_open_benchhud_blocks_the_wiring_and_writes_nothing() -> None:
    host = FakeHost()
    host.benchhud_open = True

    code, ui = setup(host)

    assert code == EXIT_FAILED
    assert host.config == {}
    assert not host.ran("benchhud", "config", "set")
    assert any("Quit it" in line for line in ui.err)
    # The services themselves are up; a re-run after quitting only wires.
    assert {"benchhud-intake", "bench-parts"} <= host.active


def test_an_open_benchhud_doesnt_matter_when_theres_nothing_to_write() -> None:
    host = FakeHost()
    setup(host)
    host.benchhud_open = True
    code, _ = setup(host)
    assert code == EXIT_OK


def test_without_benchhud_the_services_are_still_set_up() -> None:
    host = FakeHost()
    host.commands.discard("benchhud")

    code, ui = setup(host)

    assert code == EXIT_OK
    assert {"benchhud-intake", "bench-parts"} <= host.active
    assert sum("benchhud isn't installed" in line for line in ui.out) == 1
    assert not [c for who, c, _ in host.calls if who == "aaron"]


def test_no_desktop_user_is_a_failure_that_says_what_to_do() -> None:
    host = FakeHost()
    host.sudo = None  # a root shell, not sudo

    code, ui = setup(host)

    assert code == EXIT_FAILED
    assert any("--user" in line for line in ui.err)
    assert host.config == {}


def test_user_flag_picks_whose_benchhud() -> None:
    host = FakeHost()
    host.sudo = None
    host.users.add("streamer")

    code, _ = setup(host, user="streamer")

    assert code == EXIT_OK
    assert {who for who, cmd, _ in host.calls if cmd[0] == "benchhud"} == {"streamer"}


def test_an_unknown_user_is_a_failure() -> None:
    host = FakeHost()
    code, ui = setup(host, user="nobody-here")
    assert code == EXIT_FAILED
    assert any("nobody-here" in line for line in ui.err)


# -- running it wrong -----------------------------------------------------


def test_it_needs_root() -> None:
    host = FakeHost()
    host.root = False
    code, ui = setup(host)
    assert code == EXIT_USAGE
    assert host.calls == []
    assert any("sudo" in line for line in ui.err)


def test_non_interactive_with_no_password_fails_and_sets_nothing() -> None:
    host = FakeHost()
    ui = FakeUI(password=None)

    code, ui = setup(host, ui, interactive=False)

    assert code == EXIT_FAILED
    assert host.changes() == []
    assert any("Pipe one in" in line for line in ui.err)
    assert ui.password_asks == 1, "asked once, not once per service"


def test_services_that_arent_installed_are_skipped() -> None:
    host = FakeHost()
    host.commands.discard("bench-parts")

    code, ui = setup(host)

    assert code == EXIT_OK
    assert "bench-parts: not installed (sudo apt install bench-parts)" in ui.out
    assert not host.ran("bench-parts")
    assert "intake" in host.config


def test_nothing_installed_is_a_failure() -> None:
    host = FakeHost()
    host.commands = {"benchhud"}
    code, ui = setup(host)
    assert code == EXIT_FAILED
    assert any("apt install bench-suite" in line for line in ui.err)


def test_a_failing_service_command_is_reported_without_its_input() -> None:
    host = FakeHost()
    ui = FakeUI(password="   x")  # non-blank to bench-setup...
    original = host.run

    def run(cmd, stdin=None):
        if tuple(cmd)[1:] == ("set-password", "--stdin"):
            host.calls.append(("root", tuple(cmd), stdin))
            return 1, "", "Traceback...\nPermissionError: settings.env\n"
        return original(cmd, stdin)

    host.run = run  # type: ignore[method-assign]
    code, ui = setup(host, ui)

    assert code == EXIT_FAILED
    assert any("set-password failed: PermissionError" in line for line in ui.err)
    assert "   x" not in ui.text


# -- the command line -----------------------------------------------------


def test_cli_passes_the_flags_through(monkeypatch) -> None:
    seen: list[Options] = []
    monkeypatch.setattr(services, "run", lambda options: seen.append(options) or 0)

    assert main(["services", "--non-interactive", "--recon", "--user", "streamer"]) == 0
    assert main(["services"]) == 0

    assert seen == [
        Options(interactive=False, recon=True, user="streamer"),
        Options(interactive=True, recon=None, user=None),
    ]


def test_cli_refuses_recon_and_no_recon_together(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["services", "--recon", "--no-recon"])
    assert exc.value.code == 2
