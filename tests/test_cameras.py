"""``bench-setup cameras`` against a fake machine (bench-suite#24).

The fake has three USB cameras, a benchhud that knows which is the scope and
which the thermal, and a ``benchhud udev-rules`` that answers the way the real
one does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bench_setup import cameras, hostsetup
from bench_setup.cameras import thermal_rule
from bench_setup.cli import main
from bench_setup.hostsetup import CAMERA_RULES, MARKER, THERMAL_RULE
from bench_setup.services import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from test_hostsetup import BareHost
from test_services import FakeUI

SCOPE = "/dev/v4l/by-id/usb-HY_Microscope_0001-video-index0"
THERMAL = "/dev/v4l/by-id/usb-Sipeed_Ltd._Thermal_Camera__UVC__0123-video-index0"
WEBCAM = "/dev/v4l/by-id/usb-046d_C922_Pro_Stream_Webcam_ABCD-video-index0"
LABELS = {
    SCOPE: "HY Microscope: HY Microscope",
    THERMAL: "Thermal Camera (UVC): UVC Camer",
    WEBCAM: "C922 Pro Stream Webcam",
}
NAMES = {SCOPE: "scope", THERMAL: "thermal", WEBCAM: "c922-pro-stream-webcam"}


class CamHost(BareHost):
    def __init__(self) -> None:
        super().__init__()
        self.links_by_id = [WEBCAM, SCOPE, THERMAL]
        self.cams = []
        self.config["panels"] = {"scope": {"device": SCOPE}, "thermal": {"device": THERMAL}}
        self.ids: dict[str, tuple[str, str]] = {THERMAL: ("359f", "4201")}
        self.rules_fail = False

    def glob(self, pattern: str) -> list[str]:
        if pattern == cameras.BY_ID:
            return sorted(self.links_by_id)
        return super().glob(pattern)

    def camera_label(self, device: str) -> str | None:
        return LABELS.get(device)

    def same_device(self, a: str, b: str) -> bool:
        return bool(a) and a == b

    def usb_ids(self, device: str) -> tuple[str, str] | None:
        return self.ids.get(device)

    def _role_of(self, link: str) -> str:
        panels = self.config.get("panels", {})
        for role in ("scope", "thermal"):
            if panels.get(role, {}).get("device") == link:
                return role
        return NAMES[link] if NAMES[link] not in ("scope", "thermal") else "camera"

    def run_as_user(self, user, cmd, stdin=None):
        cmd = tuple(cmd)
        if cmd == ("benchhud", "udev-rules"):
            self.calls.append((user, cmd, stdin))
            if self.rules_fail:
                return 1, "", "benchhud udev-rules: no USB cameras under /dev/v4l/by-id\n"
            lines = ["# benchhud: fixed names for the bench cameras (#325).", ""]
            for link in sorted(self.links_by_id):
                serial = Path(link).name.removeprefix("usb-").removesuffix("-video-index0")
                lines.append(f"# /dev/cam-{self._role_of(link)}: {LABELS[link]}")
                lines.append(
                    f'SUBSYSTEM=="video4linux", ENV{{ID_BUS}}=="usb", '
                    f'ENV{{ID_SERIAL}}=="{serial}", ATTR{{index}}=="0", '
                    f'SYMLINK+="cam-{self._role_of(link)}"'
                )
            return 0, "\n".join(lines) + "\n", ""
        if cmd[:3] == ("benchhud", "config", "set") and cmd[3].count(".") == 2:
            self.calls.append((user, cmd, stdin))
            assert cmd[4] == "-"
            _, role, key = cmd[3].split(".")
            self.config.setdefault("panels", {}).setdefault(role, {})[key] = stdin or ""
            return 0, "", ""
        return super().run_as_user(user, cmd, stdin)

    def run(self, cmd, stdin=None):
        cmd = tuple(cmd)
        if cmd[0] == "udevadm":
            self.calls.append(("root", cmd, stdin))
            if cmd[1] == "trigger" and str(CAMERA_RULES) in self.files:
                text = self.files[str(CAMERA_RULES)]
                self.cams = sorted(
                    "/dev/" + part.split('"')[1]
                    for line in text.splitlines()
                    for part in line.split("SYMLINK+=")[1:]
                )
            return 0, "", ""
        return super().run(cmd, stdin)


class Picker(FakeUI):
    def __init__(self, choices: dict[str, int | None], yes: bool = True) -> None:
        super().__init__(yes=yes)
        self.choices = choices
        self.asked: list[str] = []

    def ask_choice(self, question: str, count: int) -> int | None:
        self.asked.append(question)
        role = "scope" if "scope" in question else "thermal"
        return self.choices.get(role)


def cam_run(host: CamHost, ui: FakeUI | None = None, **options: Any) -> tuple[int, FakeUI]:
    ui = ui or Picker({}, yes=False)
    return cameras.run(cameras.Options(**options), host=host, ui=ui), ui  # type: ignore[arg-type]


def test_it_needs_root_benchhud_and_cameras() -> None:
    host = CamHost()
    host.root = False
    assert cam_run(host)[0] == EXIT_USAGE

    host = CamHost()
    host.commands.discard("benchhud")
    code, ui = cam_run(host)
    assert code == EXIT_FAILED and "benchhud isn't installed" in ui.text

    host = CamHost()
    host.links_by_id = []
    code, ui = cam_run(host)
    assert code == EXIT_FAILED and "no USB cameras are plugged in" in ui.text
    assert not host.written


def test_yes_installs_both_rules_and_reports_the_names() -> None:
    host = CamHost()
    code, ui = cam_run(host, yes=True, interactive=False)

    assert code == EXIT_OK, ui.text
    rules = host.files[str(CAMERA_RULES)]
    assert rules.startswith(MARKER + "\n# benchhud: fixed names")
    assert 'SYMLINK+="cam-scope"' in rules and 'SYMLINK+="cam-thermal"' in rules
    assert host.files[str(THERMAL_RULE)] == f"{MARKER}\n{thermal_rule('359f', '4201')}\n"
    assert [c[1] for c in host.ran("udevadm")] == ["control", "trigger", "settle"]
    assert "  1. C922 Pro Stream Webcam" in ui.out
    assert "  2. HY Microscope  (benchhud's scope)" in ui.out
    assert "  /dev/cam-c922-pro-stream-webcam" in ui.out
    assert any("Don't add a source for /dev/cam-scope or /dev/cam-thermal" in s for s in ui.out)
    assert any("/dev/cam-c922-pro-stream-webcam), not a /dev/videoN" in s for s in ui.out)


def test_the_thermal_rule_is_the_one_from_the_setup_guide() -> None:
    assert thermal_rule("359f", "4201") == (
        'ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="359f", ATTR{idProduct}=="4201", '
        'TEST=="power/control", ATTR{power/control}="on"'
    )


def test_a_second_run_changes_nothing() -> None:
    host = CamHost()
    cam_run(host, yes=True, interactive=False)
    host.calls.clear()
    host.written.clear()

    code, ui = cam_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert not host.written and not host.ran("udevadm")
    assert f"{CAMERA_RULES}: already in place" in ui.out
    assert f"{THERMAL_RULE}: already in place" in ui.out
    assert not any("suspends idle USB devices" in line for line in ui.out), "nothing to explain"


def test_rules_installed_by_hand_from_the_guide_count_as_in_place() -> None:
    host = CamHost()
    host.files[str(THERMAL_RULE)] = thermal_rule("359f", "4201") + "\n"
    cam_run(host, yes=True, interactive=False)
    assert str(THERMAL_RULE) not in host.written
    assert not host.files[str(THERMAL_RULE)].startswith(MARKER)


def test_a_new_camera_replaces_the_rules_after_showing_them() -> None:
    host = CamHost()
    host.links_by_id = [SCOPE, THERMAL]
    cam_run(host, yes=True, interactive=False)
    host.links_by_id.append(WEBCAM)
    host.written.clear()

    code, ui = cam_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert host.written == [str(CAMERA_RULES)]
    assert f"  would replace {CAMERA_RULES}:" in ui.out
    assert "/dev/cam-c922-pro-stream-webcam" in host.cams


def test_saying_no_or_not_being_asked_changes_nothing() -> None:
    host = CamHost()
    code, ui = cam_run(host, Picker({}, yes=False))
    assert code == EXIT_OK
    assert not host.written and not host.ran("udevadm")
    assert ui.questions == ["Give the cameras these names?", "Keep the thermal camera awake?"]

    host = CamHost()
    code, ui = cam_run(host, interactive=False)
    assert "only reports" in ui.out[0]
    assert not host.written and not host.ran("udevadm")


def test_it_asks_which_camera_is_which_when_benchhud_does_not_know() -> None:
    host = CamHost()
    host.config["panels"] = {}
    ui = Picker({"scope": 1, "thermal": 2})  # sorted: webcam, scope, thermal
    code, ui = cam_run(host, ui)

    assert code == EXIT_OK, ui.text
    assert ui.asked == ["Which one is the scope camera?", "Which one is the thermal camera?"]
    assert host.config["panels"]["scope"]["device"] == SCOPE
    assert host.config["panels"]["thermal"]["device"] == THERMAL
    sets = [(c, s) for _, c, s in host.calls if c[:3] == ("benchhud", "config", "set")]
    assert all(c[4] == "-" for c, _ in sets), "the device goes in on stdin"
    assert 'SYMLINK+="cam-thermal"' in host.files[str(CAMERA_RULES)]
    assert str(THERMAL_RULE) in host.written


def test_enter_means_none_and_no_thermal_means_no_thermal_rule() -> None:
    host = CamHost()
    host.config["panels"] = {}
    code, _ = cam_run(host, Picker({"scope": 1, "thermal": None}))

    assert code == EXIT_OK
    assert "thermal" not in host.config["panels"]
    assert str(THERMAL_RULE) not in host.written
    assert str(CAMERA_RULES) in host.written


def test_it_does_not_change_benchhuds_config_while_benchhud_is_open() -> None:
    host = CamHost()
    host.config["panels"] = {}
    host.benchhud_open = True
    ui = Picker({"scope": 1, "thermal": 2})
    code, ui = cam_run(host, ui)

    assert code == EXIT_OK
    assert not ui.asked
    assert host.config["panels"] == {}
    assert any("Quit it and run this again" in line for line in ui.out)


def test_non_interactive_never_picks() -> None:
    host = CamHost()
    host.config["panels"] = {"scope": {"device": SCOPE}}
    code, ui = cam_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert any("no thermal camera among these" in line for line in ui.out)
    assert str(THERMAL_RULE) not in host.written


def test_a_thermal_camera_that_cannot_be_read_skips_its_rule() -> None:
    host = CamHost()
    host.ids.clear()
    code, ui = cam_run(host, yes=True, interactive=False)
    assert code == EXIT_OK
    assert str(THERMAL_RULE) not in host.written
    assert any("couldn't read its USB ids" in line for line in ui.out)


def test_benchhud_failing_is_reported_plainly() -> None:
    host = CamHost()
    host.rules_fail = True
    code, ui = cam_run(host, yes=True, interactive=False)
    assert code == EXIT_FAILED
    assert "benchhud udev-rules failed" in ui.text and not host.written


def test_host_undo_removes_the_camera_rules_too() -> None:
    host = CamHost()
    cam_run(host, yes=True, interactive=False)
    host.calls.clear()

    code = hostsetup.run(
        hostsetup.Options(interactive=False, yes=True, undo=True),
        host=host,  # type: ignore[arg-type]
        ui=FakeUI(),
    )

    assert code == EXIT_OK
    assert set(host.removed) == {str(CAMERA_RULES), str(THERMAL_RULE)}
    assert host.ran("udevadm", "control", "--reload")
    assert not host.ran("update-initramfs"), "no loopback file was removed"


def test_cli_passes_the_flags(monkeypatch) -> None:
    seen: list[cameras.Options] = []
    monkeypatch.setattr(cameras, "run", lambda options: seen.append(options) or 0)
    assert main(["cameras"]) == 0
    assert seen[-1] == cameras.Options()
    assert main(["cameras", "--non-interactive", "--yes", "--user", "aaron"]) == 0
    assert seen[-1] == cameras.Options(interactive=False, yes=True, user="aaron")
