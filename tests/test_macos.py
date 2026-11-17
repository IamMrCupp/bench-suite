"""bench-setup on a Mac (bench-suite#34): the menu-bar apps instead of systemd.

The fake models a Mac with the intake and parts apps and benchhud in
/Applications: each app's binary takes the service's commands, keeps its
settings in the user's Application Support folder, and reads them only when it
starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bench_setup import cameras as cameras_module
from bench_setup import doctor, guided, hostsetup, obs
from bench_setup.doctor import FAIL, OFF, OK, WARN, lsof_addresses
from bench_setup.services import BENCHHUD_MAC_MIN, EXIT_FAILED, EXIT_OK, EXIT_USAGE, SERVICES
from test_services import BY_NAME, PASSWORD, FakeHost, FakeUI, setup

APPS = {s.mac_app: s for s in SERVICES if s.mac_app}
SUPPORT = Path("/Users/aaron/Library/Application Support")
BENCHHUD_BIN = "/Applications/benchhud.app/Contents/MacOS/benchhud"
EXTENSION_ON = (
    "1 extension(s)\n--- com.apple.system_extension.cmio\n"
    "enabled\tactive\tteamID\tbundleID (version)\tname\t[state]\n"
    "*\t*\t2MMRE5MTB8\tcom.obsproject.obs-studio.mac-camera-extension (32.2.2/1)\t"
    "OBS Virtual Camera\t[activated enabled]\n"
)


class MacHost(FakeHost):
    def __init__(self) -> None:
        super().__init__()
        self.root = False
        self.sudo = None
        self.me = "aaron"
        self.commands = set()  # nothing on the PATH: it's all app bundles
        self.settings = {name: {} for name in BY_NAME}
        self.enabled = set()
        self.versions: dict[str, tuple[int, ...]] = {
            "benchhud intake": (0, 4, 0),
            "bench parts": (0, 5, 0),
            "benchhud": BENCHHUD_MAC_MIN,
            "OBS": (32, 2, 2),
        }
        self.running: set[str] = set()
        self.declines: set[str] = set(APPS)  # apps that turn down an Apple event quit
        self.stubborn: set[str] = set()  # apps that ignore a SIGTERM too
        self.fresh: set[str] = set()  # downloaded, never opened: still quarantined
        self.asking: set[str] = set()  # macOS is holding `open` on a question
        self.extensions = EXTENSION_ON
        self.listening: dict[int, str] = {}  # port -> address, default loopback
        self.config["output"] = {"enabled": True}

    # -- the Mac ----------------------------------------------------------

    def is_macos(self) -> bool:
        return True

    def current_user(self) -> str:
        return self.me

    def home_of(self, user: str) -> Path | None:
        return Path("/Users") / user

    def app_bundle(self, name: str, user: str | None = None) -> Path | None:
        return Path(f"/Applications/{name}.app") if name in self.versions else None

    def app_version(self, bundle: Path) -> tuple[int, ...]:
        return self.versions[bundle.stem]

    def benchhud(self, user: str | None = None) -> str | None:
        return BENCHHUD_BIN if "benchhud" in self.versions else None

    def process_running(self, name: str) -> bool:
        return name in self.running

    def quarantined(self, path: Path) -> bool:
        return path.stem in self.fresh

    def read_text(self, path: Path) -> str | None:
        if path.name == "settings.env":
            values = self.settings[path.parent.name]
            return "".join(f"{k}={v}\n" for k, v in values.items())
        return None

    def exists(self, path: Path) -> bool:
        return False

    def glob(self, pattern: str) -> list[str]:
        return []

    # -- commands ---------------------------------------------------------

    def run(self, cmd, stdin=None, timeout=None):
        cmd = tuple(cmd)
        name = Path(cmd[0]).name
        if name in APPS and "/Contents/MacOS/" in cmd[0]:
            service = APPS[name]
            self.calls.append(("me", cmd, stdin))
            if cmd[1:] == ("paths",):
                folder = SUPPORT / service.name
                return 0, f"data:     {folder}\nsettings: {folder / 'settings.env'}\n", ""
            # Record the argv as given, then let the fake service do the work.
            self.calls.pop()
            code, out, err = super().run((service.name, *cmd[1:]), stdin)
            who, _, given = self.calls.pop()
            self.calls.append((who, cmd, given))
            return code, out, err
        self.calls.append(("me", cmd, stdin))
        if name == "osascript":
            app = cmd[2].split('"')[1]
            if app in self.declines:
                return 1, "", f"execution error: {app} got an error: User canceled. (-128)"
            self.running.discard(app)
            self.active.discard(APPS[app].name)
            return 0, "", ""
        if name == "pkill":
            assert cmd[1:3] == ("-TERM", "-x")
            app = cmd[3]
            if app not in self.stubborn:
                self.running.discard(app)
                self.active.discard(APPS[app].name)
            return 0, "", ""
        if name == "open":
            app = Path(cmd[-1]).stem
            assert timeout, "open must never be allowed to wait forever"
            if app in self.asking:
                return 124, "", "open: timed out"
            self.running.add(app)
            service = APPS[app]
            # Like the real app: no password, a dialog and no server.
            if self.settings[service.name].get(service.password_key):
                self.active.add(service.name)
            return 0, "", ""
        if name == "systemextensionsctl":
            return 0, self.extensions, ""
        if name == "lsof":
            port = int(cmd[2].split(":")[1])
            address = self.listening.get(port, "127.0.0.1")
            line = f"Python 123 aaron 9u IPv4 0x1 0t0 TCP {address}:{port} (LISTEN)\n"
            return 0, "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n" + line, ""
        if name == "pgrep":
            return (0 if self.benchhud_open else 1), "", ""
        raise AssertionError(f"unexpected command: {cmd}")

    def run_as_user(self, user, cmd, stdin=None):
        assert user == self.me, "on a Mac everything runs as the person at the keyboard"
        cmd = tuple(cmd)
        if cmd[0] == BENCHHUD_BIN:
            return super().run_as_user(user, ("benchhud", *cmd[1:]), stdin)
        return self.run(cmd, stdin)


def mac(host: MacHost, ui: FakeUI | None = None, **options: Any) -> tuple[int, FakeUI]:
    return setup(host, ui, **options)


# -- services ---------------------------------------------------------------


def test_fresh_mac_sets_up_both_apps_without_sudo() -> None:
    host = MacHost()
    code, ui = mac(host)

    assert code == EXIT_OK, ui.text
    assert ui.password_asks == 1
    assert host.running == {"benchhud intake", "bench parts"}
    assert host.config["intake"] == {
        "base_url": "http://127.0.0.1:8401",
        "token": host.settings["benchhud-intake"]["INTAKE_API_TOKEN"],
    }
    assert host.config["inventory"]["base_url"] == "http://127.0.0.1:8402"
    assert not host.ran("systemctl"), "there's no systemd on a Mac"
    opened = host.ran("open")
    assert opened == [
        ("open", "-g", "/Applications/benchhud intake.app"),
        ("open", "-g", "/Applications/bench parts.app"),
    ]


def test_the_password_goes_in_before_the_app_is_opened() -> None:
    """Opened with no password, the app puts up a dialog instead of serving."""
    host = MacHost()
    mac(host)
    names = [Path(c[0]).name + " " + " ".join(c[1:2]) for _, c, _ in host.calls]
    assert names.index("benchhud intake set-password") < names.index("open -g")


def test_secrets_never_ride_in_argv_or_output() -> None:
    host = MacHost()
    code, ui = mac(host)
    assert code == EXIT_OK
    secrets = [PASSWORD] + [
        host.settings[s.name][s.token_key] for s in SERVICES if host.settings[s.name]
    ]
    for _, cmd, _ in host.calls:
        for secret in secrets:
            assert all(secret not in arg for arg in cmd)
    for secret in secrets:
        assert secret not in ui.text


def test_recon_has_no_mac_app_and_says_where_it_runs() -> None:
    host = MacHost()
    code, ui = mac(host, recon=True)
    assert code == EXIT_OK
    assert any(
        "bench-recon: has no Mac app" in line and "--compose --recon" in line for line in ui.out
    )
    assert "recon" not in host.config


def test_a_rerun_changes_nothing_and_leaves_the_apps_running() -> None:
    host = MacHost()
    mac(host)
    before = {name: dict(values) for name, values in host.settings.items()}
    host.calls.clear()

    code, ui = mac(host, FakeUI())

    assert code == EXIT_OK
    assert ui.password_asks == 0
    assert host.settings == before
    assert not host.ran("open") and not host.ran("osascript")
    assert sum("already set up" in line for line in ui.out) == 2


def test_a_running_app_that_got_a_new_token_is_quit_and_reopened() -> None:
    host = MacHost()
    mac(host)
    del host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    host.calls.clear()

    code, _ = mac(host, FakeUI())

    assert code == EXIT_OK
    assert host.ran("osascript") == [("osascript", "-e", 'tell application "bench parts" to quit')]
    assert host.ran("pkill") == [("pkill", "-TERM", "-x", "bench parts")], "it declined the ask"
    assert host.ran("open") == [("open", "-g", "/Applications/bench parts.app")]
    assert (
        host.config["inventory"]["token"] == host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    )


def test_an_app_that_quits_when_asked_is_not_killed() -> None:
    host = MacHost()
    mac(host)
    host.declines.clear()
    del host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    host.calls.clear()

    code, _ = mac(host, FakeUI())

    assert code == EXIT_OK
    assert host.ran("osascript") and not host.ran("pkill")


def test_an_app_that_will_not_quit_is_reported_not_left_stale() -> None:
    host = MacHost()
    mac(host)
    del host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    host.stubborn.add("bench parts")

    code, ui = mac(host, FakeUI())

    assert code == EXIT_FAILED
    assert any("didn't quit" in line for line in ui.err)


def test_an_app_too_old_to_take_commands_is_never_run() -> None:
    host = MacHost()
    host.versions["benchhud intake"] = (0, 3, 0)
    code, ui = mac(host)

    assert code == EXIT_OK, "parts is still set up"
    assert any("benchhud intake app is 0.3.0" in line and "needs 0.4.0" in line for line in ui.out)
    assert not [c for _, c, _ in host.calls if "benchhud intake.app" in c[0]]
    assert host.running == {"bench parts"}


def test_an_old_benchhud_app_is_not_run_either() -> None:
    host = MacHost()
    host.versions["benchhud"] = (0, 10, 7)
    code, ui = mac(host)

    assert code == EXIT_FAILED
    assert any("the benchhud app is 0.10.7" in line for line in ui.err)
    assert not [c for _, c, _ in host.calls if c[0] in {"benchhud", BENCHHUD_BIN}]


def test_no_apps_at_all_points_at_the_containers() -> None:
    host = MacHost()
    host.versions = {}
    code, ui = mac(host)
    assert code == EXIT_FAILED
    assert "bench-setup services --compose" in ui.text


def test_a_bare_root_shell_is_refused_but_sudo_from_an_account_works() -> None:
    host = MacHost()
    host.root, host.me = True, "root"
    assert mac(host)[0] == EXIT_USAGE

    host = MacHost()
    host.root, host.sudo = True, "aaron"
    host.run_as_user = lambda user, cmd, stdin=None: MacHost.run_as_user(  # type: ignore[method-assign]
        host, "aaron" if user == "aaron" else user, cmd, stdin
    )
    code, ui = mac(host)
    assert code == EXIT_OK, ui.text


# -- doctor -----------------------------------------------------------------


def check(host: MacHost) -> tuple[int, FakeUI, dict[str, tuple[str, str]]]:
    ui = FakeUI()
    code = doctor.run(doctor.Options(), host=host, ui=ui)  # type: ignore[arg-type]
    lines = {}
    for line in ui.out:
        if line.startswith("  "):
            level, rest = line.split(None, 1)
            lines[rest.split(":", 1)[0]] = (level, rest)
    return code, ui, lines


def test_doctor_on_a_healthy_mac() -> None:
    host = MacHost()
    mac(host)
    host.calls.clear()
    code, ui, lines = check(host)

    assert code == EXIT_OK, ui.text
    for name in ("benchhud-intake", "bench-parts"):
        level, text = lines[name]
        assert level == OK, text
        assert "password set, token set, benchhud wired" in text
    assert lines["bench-recon"] == (OFF, "bench-recon: no Mac app (it runs in a container)")
    assert lines["virtual camera"] == (OK, "virtual camera: OBS's camera extension is on")
    assert ui.out[-1] == "Nothing wrong."
    assert not any("sudo" in line for line in ui.out)
    assert not host.ran("open") and not host.ran("osascript"), "doctor only looks"


def test_doctor_sees_an_app_that_is_not_running() -> None:
    host = MacHost()
    mac(host)
    host.running.discard("bench parts")
    code, _, lines = check(host)
    assert code == EXIT_OK
    assert lines["bench-parts"][0] == WARN
    assert "isn't running. Run: bench-setup services" in lines["bench-parts"][1]


def test_doctor_sees_an_app_shared_on_the_lan() -> None:
    host = MacHost()
    mac(host)
    host.listening[8401] = "*"
    _, _, lines = check(host)
    level, text = lines["benchhud-intake"]
    assert level == WARN and "listens on 0.0.0.0" in text and "customer records" in text


def test_doctor_reports_the_obs_extension_states() -> None:
    host = MacHost()
    host.extensions = EXTENSION_ON.replace("[activated enabled]", "[activated waiting for user]")
    _, _, lines = check(host)
    assert lines["virtual camera"][0] == WARN
    assert "installed but not on" in lines["virtual camera"][1]

    host.extensions = "0 extension(s)\n"
    _, _, lines = check(host)
    assert "Start Virtual Camera" in lines["virtual camera"][1]

    del host.versions["OBS"]
    _, _, lines = check(host)
    assert "OBS isn't installed" in lines["virtual camera"][1]

    host.config["output"] = {"enabled": False}
    _, _, lines = check(host)
    assert lines["virtual camera"][0] == OFF


def test_doctor_fails_on_a_stale_token() -> None:
    host = MacHost()
    mac(host)
    host.config["intake"]["token"] = "an-old-one"
    code, ui, lines = check(host)
    assert code == EXIT_FAILED
    assert lines["benchhud-intake"][0] == FAIL
    assert "Run: bench-setup services" in lines["benchhud-intake"][1]
    assert "an-old-one" not in ui.text


def test_lsof_addresses() -> None:
    text = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        "Python 1 a 9u IPv4 0x1 0t0 TCP 127.0.0.1:8401 (LISTEN)\n"
        "Python 1 a 9u IPv4 0x1 0t0 TCP *:8401 (LISTEN)\n"
        "Python 1 a 9u IPv6 0x1 0t0 TCP [::1]:8401 (LISTEN)\n"
        "Python 1 a 9u IPv4 0x1 0t0 TCP 127.0.0.1:8401->127.0.0.1:5 (ESTABLISHED)\n"
    )
    assert lsof_addresses(text) == ["127.0.0.1", "0.0.0.0", "::1"]
    assert lsof_addresses("") == []


# -- the rest ---------------------------------------------------------------


def test_the_linux_only_steps_say_so_and_do_nothing() -> None:
    host = MacHost()
    ui = FakeUI()
    assert hostsetup.run(hostsetup.Options(), host=host, ui=ui) == EXIT_OK  # type: ignore[arg-type]
    assert cameras_module.run(cameras_module.Options(), host=host, ui=ui) == EXIT_OK  # type: ignore[arg-type]
    assert sum("is for a Linux bench" in line for line in ui.out) == 2
    assert not host.calls


def test_obs_on_a_mac_points_at_the_obs_virtual_camera() -> None:
    host = MacHost()
    ui = FakeUI()
    assert obs.run(obs.Options(), host=host, ui=ui) == EXIT_OK  # type: ignore[arg-type]
    assert "  OBS's camera extension is on." in ui.out
    assert any("pick OBS Virtual Camera" in line for line in ui.out)
    assert any("Don't press Start Virtual Camera" in line for line in ui.out)
    assert not any("/dev/video" in line for line in ui.out)


def test_the_guided_run_on_a_mac_needs_no_sudo() -> None:
    host = MacHost()
    ui = FakeUI()
    code = guided.run(host=host, ui=ui)  # type: ignore[arg-type]

    assert code == EXIT_OK, ui.text
    assert [line for line in ui.out if line.startswith("== ")] == [
        "== Services ==",
        "== OBS ==",
        "== Check ==",
    ]
    assert host.running == {"benchhud intake", "bench parts"}
    assert ui.out[-1] == "Nothing wrong."


def test_a_freshly_downloaded_app_is_not_opened_into_a_gatekeeper_prompt() -> None:
    """`open` on a quarantined app waits for a click that a script can't make."""
    host = MacHost()
    host.fresh.add("benchhud intake")
    code, ui = mac(host)

    assert code == EXIT_FAILED
    assert not [c for c in host.ran("open") if "benchhud intake" in c[-1]]
    assert any("Open it once from Applications" in line for line in ui.err)
    assert host.settings["benchhud-intake"]["INTAKE_API_TOKEN"], "the password and token are kept"
    assert "bench parts" in host.running, "the other app is still set up"

    host.fresh.clear()
    code, ui = mac(host, FakeUI())
    assert code == EXIT_OK, ui.text
    assert ui.password_asks == 0, "the second run needs nothing from the owner"
    assert "benchhud intake" in host.running


def test_open_is_timed_so_a_question_from_macos_cannot_hang_the_run() -> None:
    host = MacHost()
    host.asking.add("bench parts")
    code, ui = mac(host)
    assert code == EXIT_FAILED
    assert any("didn't finish opening the bench parts app" in line for line in ui.err)
