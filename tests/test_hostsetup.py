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
    UHUBCTL_SUDOERS,
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
        self.modes: dict[str, int] = {}
        # The account is in every group, and there's no uhubctl: both steps are quiet.
        self.system_groups = {"dialout", "video", "plugdev", "sudo"}
        self.user_groups = {"aaron", "sudo", "dialout", "video", "plugdev"}
        self.usermod_error = ""
        self.uhubctl_out = ""
        self.visudo_fragment_ok = True
        self.visudo_all_ok = True

    def exists(self, path: Path) -> bool:
        return str(path) in self.paths or str(path) in self.files

    def glob(self, pattern: str) -> list[str]:
        if pattern == "/dev/cam-*":
            return list(self.cams)
        return sorted(name for name in self.files if fnmatch.fnmatch(name, pattern))

    def write_file(self, path: Path, text: str, mode: int = 0o644) -> None:
        self.files[str(path)] = text
        self.written.append(str(path))
        self.modes[str(path)] = mode

    def which(self, command: str) -> str | None:
        return f"/usr/sbin/{command}" if command in self.commands else None

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
        if name == "id":
            assert cmd[1] == "-nG"
            return 0, " ".join(sorted(self.user_groups)) + "\n", ""
        if name == "getent":
            assert cmd[1] == "group"
            return (0 if cmd[2] in self.system_groups else 2), "", ""
        if name == "usermod":
            assert cmd[1] == "-aG", "-a: add to the groups, never replace them"
            if self.usermod_error:
                return 6, "", self.usermod_error
            self.user_groups |= set(cmd[2].split(","))
            return 0, "", ""
        if name == "/usr/sbin/uhubctl":
            assert len(cmd) == 1, "only lists; never switches a port"
            return 0, self.uhubctl_out, ""
        if name == "visudo":
            if cmd[1:] == ("-cf", "-"):
                assert stdin, "the file to check goes in on stdin"
                return (0 if self.visudo_fragment_ok else 1), "", "stdin:2:1: syntax error\n"
            assert cmd[1:] == ("-c",)
            return (0 if self.visudo_all_ok else 1), "", ""
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
        reads = {"modinfo", "mokutil", "ip", "benchhud", "id", "getent", "visudo"}
        reads |= {"/usr/sbin/uhubctl"}
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


# -- groups ----------------------------------------------------------------


def test_groups_already_right_says_so_and_asks_nothing() -> None:
    host = BareHost()
    _, ui = host_run(host, FakeUI(yes=False))
    assert "groups: aaron is already in dialout, video and plugdev" in ui.out
    assert not any("Add aaron" in q for q in ui.questions)


def test_groups_adds_only_the_missing_ones() -> None:
    host = BareHost()
    host.user_groups -= {"dialout", "plugdev"}
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert ("usermod", "-aG", "dialout,plugdev", "aaron") in [c for _, c, _ in host.calls]
    assert "log out and back in" in ui.text


def test_groups_no_changes_nothing() -> None:
    host = BareHost()
    host.user_groups -= {"video"}
    code, ui = host_run(host, FakeUI(yes=False))

    assert code == EXIT_OK
    assert "Add aaron to video?" in ui.questions
    assert not host.ran("usermod")


def test_groups_skips_a_group_the_system_doesnt_have() -> None:
    host = BareHost()
    host.system_groups -= {"plugdev"}
    host.user_groups -= {"plugdev"}
    _, ui = host_run(host, FakeUI(yes=False))
    assert "groups: aaron is already in dialout and video" in ui.out


def test_groups_failure_is_reported() -> None:
    host = BareHost()
    host.user_groups -= {"dialout"}
    host.usermod_error = "usermod: cannot lock /etc/group; try again later.\n"
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_FAILED
    assert any("cannot lock /etc/group" in line for line in ui.err)


# -- uhubctl ---------------------------------------------------------------

PPPS = (
    "Current status for hub 1-4 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]\n"
    "  Port 1: 0103 power enable connect []\n"
    "  Port 4: 0503 power highspeed enable connect [2bdf:0102 Thermal Camera (UVC)]\n"
)
GANGED = "Current status for hub 3 [1d6b:0003 xhci-hcd, USB 3.00, 2 ports, ganged]\n"
SUDOERS = str(UHUBCTL_SUDOERS)


def uhub_host(out: str = PPPS) -> BareHost:
    host = BareHost()
    host.commands |= {"uhubctl"}
    host.uhubctl_out = out
    return host


def test_uhubctl_is_silent_without_the_tool_or_a_switchable_hub() -> None:
    for host in (BareHost(), uhub_host(GANGED), uhub_host("")):
        _, ui = host_run(host, yes=True, interactive=False)
        assert "uhubctl" not in ui.text
        assert SUDOERS not in host.files


def test_uhubctl_shows_the_file_and_writes_it_checked_and_private() -> None:
    host = uhub_host()
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK, ui.text
    assert f"  would write {SUDOERS}:" in ui.out
    assert host.files[SUDOERS] == f"{MARKER}\naaron ALL=(root) NOPASSWD: /usr/sbin/uhubctl\n"
    assert host.modes[SUDOERS] == 0o440
    checks = [(c, stdin) for _, c, stdin in host.calls if c[0] == "visudo"]
    assert checks == [(("visudo", "-cf", "-"), host.files[SUDOERS]), (("visudo", "-c"), None)]
    assert "reset_port" in ui.text and "1-4" in ui.text


def test_uhubctl_no_writes_nothing() -> None:
    host = uhub_host()
    code, ui = host_run(host, FakeUI(yes=False))

    assert code == EXIT_OK
    assert "Let aaron run uhubctl without a password?" in ui.questions
    assert SUDOERS not in host.files and not host.ran("visudo")


def test_uhubctl_a_file_visudo_rejects_is_never_written() -> None:
    host = uhub_host()
    host.visudo_fragment_ok = False
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_FAILED
    assert SUDOERS not in host.files
    assert any("wasn't written" in line for line in ui.err)


def test_uhubctl_is_taken_back_out_if_sudo_then_fails_its_own_check() -> None:
    host = uhub_host()
    host.visudo_all_ok = False
    code, _ = host_run(host, yes=True, interactive=False)

    assert code == EXIT_FAILED
    assert SUDOERS not in host.files and host.removed == [SUDOERS]


def test_uhubctl_leaves_a_hand_written_file_alone() -> None:
    host = uhub_host()
    host.files[SUDOERS] = "aaron ALL=(root) NOPASSWD: /usr/sbin/uhubctl\n"
    code, ui = host_run(host, yes=True, interactive=False)

    assert code == EXIT_OK
    assert SUDOERS not in host.written
    assert any("not written by bench-setup" in line for line in ui.out)

    host_run(host, yes=True, interactive=False, undo=True)
    assert SUDOERS in host.files, "undo only removes what bench-setup wrote"


def test_uhubctl_refuses_a_name_that_isnt_plain() -> None:
    host = uhub_host()
    host_run(host, yes=True, interactive=False, user="aaron ALL=(ALL) NOPASSWD: ALL #")
    assert SUDOERS not in host.files


def test_undo_removes_the_sudoers_file_it_wrote() -> None:
    host = uhub_host()
    host_run(host, yes=True, interactive=False)
    assert SUDOERS in host.files

    code, ui = host_run(host, yes=True, interactive=False, undo=True)
    assert code == EXIT_OK
    assert SUDOERS in host.removed
    assert "Groups an account was added to are kept" in ui.text
