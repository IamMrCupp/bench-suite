"""``bench-setup doctor`` against a fake machine (bench-suite#20).

Starts from a bench that ``bench-setup services`` has just set up, then breaks
one thing at a time. doctor must never change anything, and never print a secret.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench_setup import doctor
from bench_setup.cli import main
from bench_setup.doctor import FAIL, OFF, OK, WARN, listening_addresses
from bench_setup.services import EXIT_FAILED, EXIT_OK, SERVICES
from test_services import BY_NAME, FakeHost, FakeUI, setup

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"


def tcp_line(address: str, port: int, state: str = "0A") -> str:
    return f"   0: {address}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000\n"


V4_LOOPBACK = "0100007F"
V4_ANY = "00000000"
V6_ANY = "0" * 32
V6_LOOPBACK = "0" * 24 + "01000000"


class DoctorHost(FakeHost):
    """A bench after a good ``bench-setup services`` run, recon included."""

    def __init__(self, *, recon: bool = True) -> None:
        super().__init__()
        code, ui = setup(self, recon=recon)
        assert code == EXIT_OK, ui.text
        self.calls.clear()
        self.me = "aaron"
        self.binds: dict[int, list[str]] = {}  # port -> hex addresses; default loopback
        self.files: dict[str, str] = {
            "/sys/module/uvcvideo/parameters/nodrop": "0\n",
            "/proc/sys/net/ipv4/conf/all/arp_ignore": "0\n",
            "/proc/sys/net/ipv4/conf/all/arp_announce": "0\n",
        }
        self.paths = {"/dev/video10", "/sys/module/v4l2loopback"}
        self.cams = ["/dev/cam-scope"]
        self.links: list[dict[str, Any]] = [
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "enp3s0", "addr_info": [{"local": "192.168.1.20", "prefixlen": 24}]},
        ]
        self.config["output"] = {"enabled": True, "device": "/dev/video10"}

    def current_user(self) -> str:
        return self.me

    def exists(self, path: Path) -> bool:
        return str(path) in self.paths

    def glob(self, pattern: str) -> list[str]:
        assert pattern == "/dev/cam-*"
        return list(self.cams)

    def read_text(self, path: Path) -> str | None:
        name = str(path)
        if name in ("/proc/net/tcp", "/proc/net/tcp6"):
            v6 = name.endswith("6")
            text = HEADER
            for unit in self.active:
                service = BY_NAME[unit]
                port = int(self.settings[unit].get(service.port_key, service.default_port))
                for address in self.binds.get(port, [V4_LOOPBACK]):
                    if (len(address) == 32) == v6:
                        text += tcp_line(address, port)
            return text
        if path.name == "settings.env":
            if not self.root:
                return None
            values = self.settings[path.parent.name]
            return "".join(f"{k}={v}\n" for k, v in values.items()) if values else None
        return self.files.get(name)

    def run(self, cmd, stdin=None):
        cmd = tuple(cmd)
        if cmd[0] == "ip":
            self.calls.append(("root", cmd, stdin))
            return 0, json.dumps(self.links), ""
        return super().run(cmd, stdin)


def check(host: DoctorHost, **options: Any) -> tuple[int, FakeUI, dict[str, tuple[str, str]]]:
    """Run doctor. Returns the exit code, the output, and each line keyed by its subject."""
    before = (
        json.dumps(host.settings, sort_keys=True),
        json.dumps(host.config, sort_keys=True),
        set(host.enabled),
        set(host.active),
    )
    ui = FakeUI()
    code = doctor.run(doctor.Options(**options), host=host, ui=ui)
    after = (
        json.dumps(host.settings, sort_keys=True),
        json.dumps(host.config, sort_keys=True),
        set(host.enabled),
        set(host.active),
    )
    assert before == after, "doctor changed something"
    for _, cmd, stdin in host.calls:
        assert stdin is None, f"doctor fed a command stdin: {cmd}"
        assert cmd[0] in {"systemctl", "ip", "benchhud"}, cmd
        if cmd[0] == "systemctl":
            assert cmd[1] in {"is-enabled", "is-active"}, cmd
        if cmd[0] == "benchhud":
            assert cmd[1:] == ("config", "path"), cmd
    lines: dict[str, tuple[str, str]] = {}
    for line in ui.out:
        if line.startswith("  "):
            level, rest = line.split(None, 1)
            lines[rest.split(":", 1)[0]] = (level, rest)
    return code, ui, lines


def test_a_healthy_bench_has_nothing_wrong() -> None:
    host = DoctorHost()
    code, ui, lines = check(host)

    assert code == EXIT_OK, ui.text
    for service in SERVICES:
        level, text = lines[service.name]
        assert level == OK, text
        assert f"answering on 127.0.0.1:{service.default_port}" in text
        assert "password set, token set, benchhud wired" in text
    assert lines["virtual camera"] == (OK, "virtual camera: /dev/video10")
    assert lines["uvcvideo"][0] == OK
    assert lines["camera names"] == (OK, "camera names: /dev/cam-scope")
    assert ui.out[-1] == "Nothing wrong."
    assert not ui.err


def test_secrets_never_reach_the_output() -> None:
    host = DoctorHost()
    _, ui, _ = check(host)
    for service in SERVICES:
        for value in host.settings[service.name].values():
            if not value.isdigit():
                assert value not in ui.text


def test_recon_off_is_not_a_failure() -> None:
    host = DoctorHost(recon=False)
    code, _, lines = check(host)

    assert code == EXIT_OK
    assert lines["bench-recon"] == (OFF, "bench-recon: installed, off")


def test_a_service_that_is_not_installed_is_only_noted() -> None:
    host = DoctorHost()
    host.commands.discard("bench-parts")
    code, _, lines = check(host)

    assert code == EXIT_OK
    assert lines["bench-parts"] == (OFF, "bench-parts: not installed")


def test_intake_installed_but_never_turned_on_is_a_warning() -> None:
    host = DoctorHost()
    host.enabled.discard("benchhud-intake")
    host.active.discard("benchhud-intake")
    code, ui, lines = check(host)

    assert code == EXIT_OK
    assert lines["benchhud-intake"][0] == WARN
    assert "sudo bench-setup services" in lines["benchhud-intake"][1]
    assert ui.out[-1] == "0 problems, 1 warning."


def test_enabled_but_silent_is_a_problem() -> None:
    host = DoctorHost()
    host.silent.add(8402)
    code, ui, lines = check(host)

    assert code == EXIT_FAILED
    level, text = lines["bench-parts"]
    assert level == FAIL
    assert "nothing answers on 127.0.0.1:8402" in text and "journalctl -u bench-parts" in text
    assert ui.out[-1] == "1 problem, 0 warnings."


def test_something_else_on_the_port_is_a_problem() -> None:
    host = DoctorHost()
    host.answering[8401] = "bench-parts"
    code, _, lines = check(host)

    assert code == EXIT_FAILED
    assert lines["benchhud-intake"] == (
        FAIL,
        "benchhud-intake: 127.0.0.1:8401 is answered by bench-parts",
    )


def test_it_follows_the_port_in_settings_env() -> None:
    host = DoctorHost()
    host.settings["benchhud-intake"]["INTAKE_PORT"] = "8000"
    host.config["intake"]["base_url"] = "http://127.0.0.1:8000"
    code, _, lines = check(host)

    assert code == EXIT_OK
    assert "answering on 127.0.0.1:8000" in lines["benchhud-intake"][1]


def test_recon_listening_beyond_this_machine_is_a_problem() -> None:
    host = DoctorHost()
    host.binds[8403] = [V4_ANY]
    code, _, lines = check(host)

    assert code == EXIT_FAILED
    level, text = lines["bench-recon"]
    assert level == FAIL
    assert "listens on 0.0.0.0, beyond this machine" in text
    assert "client engagement data" in text


def test_intake_on_the_lan_is_a_warning_that_names_what_it_holds() -> None:
    host = DoctorHost()
    host.binds[8401] = [V6_ANY]
    code, _, lines = check(host)

    assert code == EXIT_OK
    level, text = lines["benchhud-intake"]
    assert level == WARN
    assert "listens on ::" in text and "customer records" in text


def test_ipv6_loopback_counts_as_this_machine() -> None:
    host = DoctorHost()
    host.binds[8402] = [V4_LOOPBACK, V6_LOOPBACK]
    code, _, lines = check(host)

    assert code == EXIT_OK
    assert lines["bench-parts"][0] == OK


def test_a_missing_token_is_a_problem() -> None:
    host = DoctorHost()
    del host.settings["bench-parts"]["BENCH_PARTS_API_TOKEN"]
    code, _, lines = check(host)

    assert code == EXIT_FAILED
    assert lines["bench-parts"] == (
        FAIL,
        "bench-parts: no token set. Run: sudo bench-setup services",
    )


def test_benchhud_with_a_stale_token_is_a_problem() -> None:
    host = DoctorHost()
    host.config["inventory"]["token"] = "an-old-one"
    code, ui, lines = check(host)

    assert code == EXIT_FAILED
    assert lines["bench-parts"][0] == FAIL
    assert "isn't the service's token" in lines["bench-parts"][1]
    assert "an-old-one" not in ui.text


def test_benchhud_pointing_at_the_wrong_port_is_a_problem() -> None:
    host = DoctorHost()
    host.config["intake"]["base_url"] = "http://127.0.0.1:8000"
    code, _, lines = check(host)

    assert code == EXIT_FAILED
    assert (
        "benchhud points at http://127.0.0.1:8000, not http://127.0.0.1:8401"
        in (lines["benchhud-intake"][1])
    )


def test_benchhud_not_wired_at_all_is_a_problem() -> None:
    host = DoctorHost()
    del host.config["recon"]
    code, _, lines = check(host)

    assert code == EXIT_FAILED
    assert "benchhud has no token for it" in lines["bench-recon"][1]


def test_no_benchhud_means_no_wiring_or_camera_output_to_check() -> None:
    host = DoctorHost()
    host.commands.discard("benchhud")
    code, ui, lines = check(host)

    assert code == EXIT_OK, ui.text
    assert "benchhud" not in lines["bench-parts"][1].split(":", 1)[1]
    assert "virtual camera" not in lines


def test_without_sudo_it_checks_what_it_can_and_says_so() -> None:
    host = DoctorHost()
    host.root, host.sudo = False, None
    code, ui, lines = check(host)

    assert code == EXIT_OK, ui.text
    level, text = lines["benchhud-intake"]
    assert level == OK
    assert "password and token not checked (needs sudo)" in text
    assert "benchhud has its URL and a token" in text
    assert "Run it with sudo" in ui.out[-2]
    assert {who for who, cmd, _ in host.calls if cmd[0] == "benchhud"} == {"aaron"}


def test_a_missing_loopback_node() -> None:
    host = DoctorHost()
    host.config["output"]["thermal_device"] = "/dev/video13"
    code, _, lines = check(host)
    assert code == EXIT_OK
    level, text = lines["virtual camera"]
    assert level == WARN
    assert "/dev/video13 missing though v4l2loopback is loaded" in text
    assert "update-initramfs -u" in text

    host.paths.clear()
    _, _, lines = check(host)
    assert (
        "/dev/video10, /dev/video13 missing, and v4l2loopback isn't loaded"
        in (lines["virtual camera"][1])
    )


def test_output_switched_off_is_not_a_warning() -> None:
    host = DoctorHost()
    host.config["output"] = {"enabled": False, "device": "/dev/video10"}
    host.paths.clear()
    code, _, lines = check(host)

    assert code == EXIT_OK
    assert lines["virtual camera"][0] == OFF


def test_nodrop_left_on_is_a_warning() -> None:
    host = DoctorHost()
    host.files["/sys/module/uvcvideo/parameters/nodrop"] = "1\n"
    code, _, lines = check(host)

    assert code == EXIT_OK
    assert lines["uvcvideo"][0] == WARN and "nodrop=1" in lines["uvcvideo"][1]


def test_no_cameras_ever_plugged_in_says_nothing_about_cameras() -> None:
    host = DoctorHost()
    del host.files["/sys/module/uvcvideo/parameters/nodrop"]
    host.cams = []
    _, _, lines = check(host)

    assert "uvcvideo" not in lines and "camera names" not in lines


def test_cameras_without_stable_names_and_a_thermal_cam_without_its_rule() -> None:
    host = DoctorHost()
    host.cams = []
    _, _, lines = check(host)
    assert lines["camera names"][0] == WARN
    assert "benchhud udev-rules" in lines["camera names"][1]

    host.cams = ["/dev/cam-scope", "/dev/cam-thermal"]
    host.paths.add("/dev/cam-thermal")
    _, _, lines = check(host)
    assert lines["thermal camera"][0] == WARN
    assert "60-benchhud-thermal.rules" in lines["thermal camera"][1]

    host.paths.add("/etc/udev/rules.d/60-benchhud-thermal.rules")
    _, _, lines = check(host)
    assert "thermal camera" not in lines


def test_wired_and_wifi_on_one_subnet() -> None:
    host = DoctorHost()
    host.links.append(
        {"ifname": "wlp4s0", "addr_info": [{"local": "192.168.1.31", "prefixlen": 24}]}
    )
    host.links.append(
        {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1", "prefixlen": 16}]}
    )
    code, _, lines = check(host)
    assert code == EXIT_OK
    level, text = lines["network"]
    assert level == WARN
    assert "enp3s0 and wlp4s0 are both on 192.168.1.0/24" in text
    assert "arp_ignore=0" in text

    host.files["/proc/sys/net/ipv4/conf/all/arp_ignore"] = "1\n"
    host.files["/proc/sys/net/ipv4/conf/all/arp_announce"] = "2\n"
    _, _, lines = check(host)
    assert lines["network"][0] == OK


def test_one_interface_per_subnet_says_nothing() -> None:
    _, _, lines = check(DoctorHost())
    assert "network" not in lines


def test_listening_addresses_reads_the_kernels_tables() -> None:
    v4 = (
        HEADER
        + tcp_line(V4_LOOPBACK, 8403)
        + tcp_line(V4_ANY, 22)
        + tcp_line("0A01A8C0", 8403, "01")
    )
    assert listening_addresses(v4, 8403) == ["127.0.0.1"]
    assert listening_addresses(v4, 22) == ["0.0.0.0"]
    v6 = HEADER + tcp_line(V6_LOOPBACK, 8403) + tcp_line(V6_ANY, 8403)
    assert listening_addresses(v6, 8403) == ["::1", "::"]
    assert listening_addresses("garbage\nmore garbage\n", 8403) == []


def test_cli_runs_doctor(monkeypatch) -> None:
    seen: list[doctor.Options] = []
    monkeypatch.setattr(doctor, "run", lambda options: seen.append(options) or 0)

    assert main(["doctor"]) == 0
    assert seen[-1].user is None
    assert main(["doctor", "--user", "aaron"]) == 0
    assert seen[-1].user == "aaron"


def test_sudo_is_only_suggested_when_it_would_show_more() -> None:
    """A bench with none of the services installed: sudo has nothing to add."""
    host = DoctorHost()
    host.root, host.sudo = False, None
    host.commands -= {"benchhud-intake", "bench-parts", "bench-recon"}
    _, ui, _ = check(host)
    assert not any("sudo" in line for line in ui.out)

    host = DoctorHost()
    _, ui, _ = check(host)  # already root
    assert not any("Run it with sudo" in line for line in ui.out)
