"""``bench-setup host`` against a fake machine (bench-suite#22).

The fake keeps a little filesystem and records every command, so each test can
say exactly what was written, what was run, and that a run which wasn't told
yes changed nothing.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from bench_setup import hostsetup
from bench_setup.cli import main
from bench_setup.hostsetup import (
    ARP_SYSCTL,
    LOOPBACK_LOAD,
    LOOPBACK_OPTIONS,
    MARKER,
    NODROP_OPTIONS,
    loopback_options,
)
from bench_setup.services import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from test_doctor import DoctorHost
from test_services import FakeUI

NODROP = "/sys/module/uvcvideo/parameters/nodrop"
ARP_DIR = "/proc/sys/net/ipv4/conf/all"
WIFI = {"ifname": "wlp4s0", "addr_info": [{"local": "192.168.1.31", "prefixlen": 24}]}


class BareHost(DoctorHost):
    """A bench with the services set up and none of the host steps done."""

    def __init__(self) -> None:
        super().__init__()
        self.paths = set()  # no /dev/video10, module not loaded
        self.files[NODROP] = "1\n"
        self.module_available = False
        self.secure_boot = False
        self.modprobe_error = ""
        self.commands |= {"update-initramfs"}
        self.written: list[str] = []
        self.removed: list[str] = []

    def exists(self, path: Path) -> bool:
        return str(path) in self.paths or str(path) in self.files

    def glob(self, pattern: str) -> list[str]:
        if pattern == "/dev/cam-*":
            return list(self.cams)
        return sorted(name for name in self.files if fnmatch.fnmatch(name, pattern))

    def write_file(self, path: Path, text: str) -> None:
        self.files[str(path)] = text
        self.written.append(str(path))

    def write_value(self, path: Path, value: str) -> bool:
        self.files[str(path)] = value + "\n"
        return True

    def remove(self, path: Path) -> None:
        del self.files[str(path)]
        self.removed.append(str(path))

    def run(self, cmd, stdin=None):
        cmd = tuple(cmd)
        name = cmd[0]
        if name in {"systemctl", "ip", "pgrep"} or name.startswith("bench"):
            return super().run(cmd, stdin)
        self.calls.append(("root", cmd, stdin))
        if name == "modinfo":
            return (0 if self.module_available else 1), "", ""
        if name == "apt-get":
            self.module_available = True
            return 0, "", ""
        if name == "update-initramfs":
            return 0, "", ""
        if name == "mokutil":
            return 0, f"SecureBoot {'enabled' if self.secure_boot else 'disabled'}\n", ""
        if name == "modprobe":
            if self.modprobe_error:
                return 1, "", self.modprobe_error
            self.paths |= {"/sys/module/v4l2loopback"}
            line = self.files[str(LOOPBACK_OPTIONS)].splitlines()[1]
            numbers = line.split("video_nr=")[1].split()[0].split(",")
            self.paths |= {f"/dev/video{n}" for n in numbers}
            return 0, "", ""
        if name == "sysctl":
            assert cmd[1:] == ("-q", "-p", str(ARP_SYSCTL))
            self.files[f"{ARP_DIR}/arp_ignore"] = "1\n"
            self.files[f"{ARP_DIR}/arp_announce"] = "2\n"
            return 0, "", ""
        raise AssertionError(f"unexpected command: {cmd}")

    def changed(self) -> list[tuple[str, ...]]:
        reads = {"modinfo", "mokutil", "ip", "benchhud"}
        return [c for _, c, _ in self.calls if c[0] not in reads]


def host_run(host: BareHost, ui: FakeUI | None = None, **options: Any) -> tuple[int, FakeUI]:
    ui = ui or FakeUI()
    return hostsetup.run(hostsetup.Options(**options), host=host, ui=ui), ui  # type: ignore[arg-type]


def test_it_needs_root() -> None:
    host = BareHost()
    host.root = False
    code, ui = host_run(host)
    assert code == EXIT_USAGE
    assert "sudo" in ui.text and not host.written


def test_saying_no_to_everything_changes_nothing() -> None:
    host = BareHost()
    host.links.append(WIFI)
    ui = FakeUI(yes=False)
    code, ui = host_run(host, ui)

    assert code == EXIT_OK
    assert not host.written and not host.changed()
    assert [q for q in ui.questions if not q.startswith("  ")] == [
        "Set up the virtual camera?",
        "Have the driver drop torn frames?",
        "Write it and apply it now?",
    ]
    assert host.files[NODROP] == "1\n"


def test_it_shows_each_file_before_asking() -> None:
    host = BareHost()
    _, ui = host_run(host, FakeUI(yes=False))
    assert f"  would write {LOOPBACK_OPTIONS}:" in ui.out
    assert f"    {MARKER}" in ui.out
    assert "    options v4l2loopback exclusive_caps=1 card_label=benchhud video_nr=10" in ui.out
    assert f"  would write {NODROP_OPTIONS}:" in ui.out


def test_non_interactive_without_yes_only_reports() -> None:
    host = BareHost()
    host.links.append(WIFI)
    code, ui = host_run(host, interactive=False)

    assert code == EXIT_OK
    assert "only reports" in ui.out[0]
    assert not host.written and not host.changed()


def test_yes_sets_up_the_virtual_camera_end_to_end() -> None:
    host = BareHost()
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK, ui.text
    assert host.ran("apt-get", "install", "-y", "-q", "v4l2loopback-dkms", "v4l2loopback-utils")
    assert host.files[str(LOOPBACK_OPTIONS)] == (
        f"{MARKER}\noptions v4l2loopback exclusive_caps=1 card_label=benchhud video_nr=10\n"
    )
    assert host.files[str(LOOPBACK_LOAD)] == f"{MARKER}\nv4l2loopback\n"
    order = [c[0] for c in host.changed()]
    assert order.index("update-initramfs") < order.index("modprobe"), "initramfs before loading"
    assert "/dev/video10" in host.paths
    assert "  loaded: /dev/video10 is there" in ui.out


def test_a_second_run_finds_it_all_done() -> None:
    host = BareHost()
    host.links.append(WIFI)
    host_run(host, yes=True, interactive=False)
    host.calls.clear()
    host.written.clear()

    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert not host.written and not host.changed()
    assert "virtual camera: /dev/video10 already there" in ui.out
    assert any("already drops torn frames" in line for line in ui.out)
    assert any("ARP settings are already in place" in line for line in ui.out)


def test_the_module_is_not_installed_again_when_it_is_there() -> None:
    host = BareHost()
    host.module_available = True
    host_run(host, yes=True, interactive=False)
    assert not host.ran("apt-get")


def test_loading_is_asked_separately_and_can_wait_for_a_reboot() -> None:
    host = BareHost()

    class Picky(FakeUI):
        def ask_yes_no(self, question: str) -> bool:
            self.questions.append(question)
            return not question.startswith("Load the module now?") and not question.startswith("  ")

    code, ui = host_run(host, Picky())

    assert code == EXIT_OK
    assert str(LOOPBACK_OPTIONS) in host.written
    assert not host.ran("modprobe")
    assert "  it loads at the next boot" in ui.out


def test_layouts() -> None:
    assert loopback_options(2) == (
        "options v4l2loopback devices=2 video_nr=10,11 "
        "card_label=benchhud,OBS-Virtual-Camera exclusive_caps=1,1"
    )
    assert loopback_options(4) == (
        "options v4l2loopback devices=4 video_nr=10,11,12,13 "
        "card_label=benchhud,OBS-Virtual-Camera,benchhud-scope,benchhud-thermal "
        "exclusive_caps=1,1,1,1"
    )

    host = BareHost()
    host_run(host, yes=True, interactive=False, nodes=2)
    assert "devices=2" in host.files[str(LOOPBACK_OPTIONS)]

    # benchhud's config already names a feed node: four, without being told.
    host = BareHost()
    host.config["output"]["thermal_device"] = "/dev/video13"
    host_run(host, yes=True, interactive=False)
    assert "devices=4" in host.files[str(LOOPBACK_OPTIONS)]
    assert {"/dev/video10", "/dev/video13"} <= host.paths

    # Asked, and the answer picks it.
    host = BareHost()
    ui = FakeUI(yes=True)
    host_run(host, ui)
    assert "  Separate scope and thermal feeds for OBS as well?" in ui.questions
    assert "devices=4" in host.files[str(LOOPBACK_OPTIONS)]

    code, ui = host_run(BareHost(), nodes=3)
    assert code == EXIT_USAGE


def test_a_hand_written_options_file_is_left_alone() -> None:
    host = BareHost()
    mine = "options v4l2loopback devices=4 video_nr=10,11,12,13 exclusive_caps=1,1,1,1\n"
    host.files[str(LOOPBACK_OPTIONS)] = mine
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert host.files[str(LOOPBACK_OPTIONS)] == mine
    assert str(LOOPBACK_OPTIONS) not in host.written
    assert not host.ran("modprobe") and not host.ran("apt-get")
    assert any("left alone" in line and "update-initramfs -u" in line for line in ui.out)


def test_a_commented_out_options_line_does_not_count() -> None:
    host = BareHost()
    host.files["/etc/modprobe.d/notes.conf"] = "# options v4l2loopback video_nr=10\n"
    host_run(host, yes=True, interactive=False)
    assert str(LOOPBACK_OPTIONS) in host.written


def test_a_module_already_loaded_with_other_options_is_not_reloaded() -> None:
    host = BareHost()
    host.paths.add("/sys/module/v4l2loopback")
    host.module_available = True
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert str(LOOPBACK_OPTIONS) in host.written
    assert not host.ran("modprobe")
    assert any("Quit both" in line for line in ui.out)


def test_secure_boot_is_explained_before_the_question() -> None:
    host = BareHost()
    host.secure_boot = True
    ui = FakeUI(yes=False)
    host_run(host, ui)
    note = next(i for i, line in enumerate(ui.out) if "Secure Boot is on" in line)
    assert "Enroll MOK" in ui.out[note]
    assert not host.written

    host = BareHost()
    host.modprobe_error = "modprobe: ERROR: could not insert 'v4l2loopback': Key was rejected\n"
    code, ui = host_run(host, yes=True, interactive=False)
    assert code == EXIT_FAILED
    assert any("finish the MOK screen" in line for line in ui.err)


def test_nodrop_is_written_for_boot_and_set_live() -> None:
    host = BareHost()
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert host.files[str(NODROP_OPTIONS)] == f"{MARKER}\noptions uvcvideo nodrop=0\n"
    assert host.files[NODROP] == "0\n"
    assert any("Restart benchhud" in line for line in ui.out)


def test_nodrop_already_set_for_boot_by_hand_is_only_applied_live() -> None:
    host = BareHost()
    host.files["/etc/modprobe.d/uvcvideo-nodrop.conf"] = "options uvcvideo nodrop=0\n"
    host_run(host, yes=True, interactive=False)

    assert str(NODROP_OPTIONS) not in host.written
    assert host.files[NODROP] == "0\n"


def test_no_camera_ever_plugged_in_skips_nodrop() -> None:
    host = BareHost()
    del host.files[NODROP]
    _, ui = host_run(host, yes=True, interactive=False)
    assert not any("uvcvideo" in line for line in ui.out)
    assert str(NODROP_OPTIONS) not in host.written


def test_arp_only_comes_up_with_two_cards_on_one_subnet() -> None:
    host = BareHost()
    _, ui = host_run(host, yes=True, interactive=False)
    assert str(ARP_SYSCTL) not in host.written
    assert not any(line.startswith("network") for line in ui.out)

    host = BareHost()
    host.links.append(WIFI)
    code, ui = host_run(host, yes=True, interactive=False)
    assert code == EXIT_OK
    text = host.files[str(ARP_SYSCTL)]
    assert text.startswith(MARKER)
    assert "net.ipv4.conf.all.arp_ignore = 1" in text
    assert "net.ipv4.conf.default.arp_announce = 2" in text
    assert host.ran("sysctl", "-q", "-p", str(ARP_SYSCTL))
    assert any("Reconnect both cards" in line for line in ui.out)


def test_a_hand_written_sysctl_file_is_left_alone() -> None:
    host = BareHost()
    host.links.append(WIFI)
    host.files["/etc/sysctl.d/60-arp-multihome.conf"] = "net.ipv4.conf.all.arp_ignore = 1\n"
    _, ui = host_run(host, yes=True, interactive=False)

    assert str(ARP_SYSCTL) not in host.written
    assert not host.ran("sysctl")
    assert any("sudo sysctl --system" in line for line in ui.out)


def test_undo_removes_only_what_it_wrote() -> None:
    host = BareHost()
    host.links.append(WIFI)
    host.files["/etc/modprobe.d/uvcvideo-nodrop.conf"] = "options uvcvideo nodrop=0\n"
    host_run(host, yes=True, interactive=False)
    assert set(host.written) == {str(LOOPBACK_OPTIONS), str(LOOPBACK_LOAD), str(ARP_SYSCTL)}
    host.calls.clear()

    code, ui = host_run(host, yes=True, interactive=False, undo=True)

    assert code == EXIT_OK
    assert set(host.removed) == set(host.written)
    assert "/etc/modprobe.d/uvcvideo-nodrop.conf" in host.files, "the hand-written one stays"
    assert host.ran("update-initramfs", "-u")
    assert not host.ran("modprobe"), "undo never unloads a module"
    assert any("stays as it is until the next reboot" in line for line in ui.out)


def test_undo_never_removes_a_hand_written_file_at_one_of_its_paths() -> None:
    host = BareHost()
    host.files[str(LOOPBACK_OPTIONS)] = "options v4l2loopback video_nr=10\n"
    code, ui = host_run(host, yes=True, interactive=False, undo=True)

    assert code == EXIT_OK
    assert not host.removed
    assert f"{LOOPBACK_OPTIONS}: not written by bench-setup, left alone" in ui.out
    assert any("Nothing to undo" in line for line in ui.out)


def test_undo_asks_first() -> None:
    host = BareHost()
    host_run(host, yes=True, interactive=False)
    code, ui = host_run(host, FakeUI(yes=False), undo=True)
    assert code == EXIT_OK
    assert ui.questions == ["Remove them?"]
    assert not host.removed


def test_cli_passes_the_flags(monkeypatch) -> None:
    seen: list[hostsetup.Options] = []
    monkeypatch.setattr(hostsetup, "run", lambda options: seen.append(options) or 0)

    assert main(["host"]) == 0
    assert seen[-1] == hostsetup.Options()
    assert main(["host", "--non-interactive", "--yes", "--nodes", "4", "--user", "aaron"]) == 0
    assert seen[-1] == hostsetup.Options(interactive=False, yes=True, nodes=4, user="aaron")
    assert main(["host", "--undo"]) == 0
    assert seen[-1].undo is True
