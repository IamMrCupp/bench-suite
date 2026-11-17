"""``bench-setup doctor``: look at the bench and say what's wrong. Changes nothing.

Two halves. The services: installed, on, answering as themselves, listening
only where they should, and wired into benchhud. The host: the things
benchhud's BENCH-SETUP.md has you set up by hand, checked the way that
document says to check them.

A secret is only ever reported as set or missing, never printed.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench_setup.host import Host, parse_settings
from bench_setup.services import (
    EXIT_FAILED,
    EXIT_OK,
    SERVICES,
    UI,
    Service,
    TerminalUI,
    benchhud_mac_too_old,
)

OK, OFF, WARN, FAIL = "ok", "off", "warn", "FAIL"

_LISTEN = "0A"
NODROP = Path("/sys/module/uvcvideo/parameters/nodrop")
LOOPBACK_MODULE = Path("/sys/module/v4l2loopback")
_THERMAL_CAM = Path("/dev/cam-thermal")
_THERMAL_RULE = Path("/etc/udev/rules.d/60-benchhud-thermal.rules")
OBS_EXTENSION = "com.obsproject.obs-studio.mac-camera-extension"
ARP = Path("/proc/sys/net/ipv4/conf/all")
DEFAULT_OUTPUT = "/dev/video10"


@dataclass(frozen=True)
class Options:
    #: Whose benchhud to look at; None means whoever ran sudo, else whoever runs this.
    user: str | None = None


@dataclass
class Report:
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.lines.append((level, text))

    def count(self, level: str) -> int:
        return sum(1 for found, _ in self.lines if found == level)


def listening_addresses(text: str, port: int) -> list[str]:
    """The local addresses with a listening socket on ``port``, from /proc/net/tcp or tcp6."""
    found: list[str] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != _LISTEN:
            continue
        raw, _, raw_port = parts[1].rpartition(":")
        try:
            if int(raw_port, 16) != port:
                continue
            # The kernel prints each 32-bit word of the address in host byte order.
            words = [bytes.fromhex(raw[i : i + 8])[::-1] for i in range(0, len(raw), 8)]
            found.append(str(ipaddress.ip_address(b"".join(words))))
        except ValueError:
            continue
    return found


def lsof_addresses(text: str) -> list[str]:
    """The listening addresses in ``lsof -nP -iTCP:PORT -sTCP:LISTEN`` output."""
    found = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[-1] != "(LISTEN)":
            continue
        address = parts[-2].rpartition(":")[0].strip("[]")
        if address == "*":
            address = "0.0.0.0"
        try:
            found.append(str(ipaddress.ip_address(address)))
        except ValueError:
            continue
    return found


def obs_extension_state(host: Host) -> tuple[str, str]:
    """Whether macOS lets OBS's camera extension run, as a report level and a sentence."""
    where = "System Settings > General > Login Items & Extensions > Camera Extensions"
    if host.app_bundle("OBS") is None:
        return WARN, "OBS isn't installed, and benchhud's picture reaches a stream through it"
    code, out, _ = host.run(["systemextensionsctl", "list"])
    line = next((ln for ln in out.splitlines() if OBS_EXTENSION in ln), "") if code == 0 else ""
    if "[activated enabled]" in line:
        return OK, "OBS's camera extension is on"
    if line:
        return WARN, f"OBS's camera extension is installed but not on. Turn it on in {where}"
    return (
        WARN,
        "OBS's camera extension isn't installed yet. Open OBS, press Start Virtual Camera "
        f"once, allow it in {where}, then stop it again",
    )


def _beyond_loopback(addresses: list[str]) -> list[str]:
    return sorted({a for a in addresses if not ipaddress.ip_address(a).is_loopback})


def benchhud_config(host: Host, user: str | None) -> dict[str, Any] | None:
    """``user``'s benchhud config, or None when there's no benchhud (or no one to ask as)."""
    benchhud = host.benchhud(user)
    if benchhud is None or user is None or not host.user_exists(user):
        return None
    if benchhud_mac_too_old(host, user) is not None:
        return None  # an old Mac app would open its window instead of answering
    code, out, _ = host.run_as_user(user, [benchhud, "config", "path"])
    if code != 0 or not out.strip():
        return None
    return host.read_toml(Path(out.strip()))


def output_nodes(config: dict[str, Any]) -> list[str]:
    """The loopback nodes benchhud's ``[output]`` writes to."""
    output = config.get("output")
    output = output if isinstance(output, dict) else {}
    nodes = [output.get("device") or DEFAULT_OUTPUT]
    return nodes + [output[k] for k in ("scope_device", "thermal_device") if output.get(k)]


def shared_subnets(host: Host) -> dict[str, list[str]]:
    """Subnets with more than one interface up on them: ``{network: [interfaces]}``."""
    code, out, _ = host.run(["ip", "-j", "-4", "addr", "show", "up"])
    if code != 0:
        return {}
    try:
        links = json.loads(out)
    except ValueError:
        return {}
    nets: dict[str, set[str]] = {}
    for link in links if isinstance(links, list) else []:
        name = link.get("ifname", "")
        for info in link.get("addr_info", []):
            try:
                iface = ipaddress.ip_interface(f"{info['local']}/{info['prefixlen']}")
            except (KeyError, ValueError):
                continue
            if iface.is_loopback or iface.is_link_local:
                continue
            nets.setdefault(str(iface.network), set()).add(name)
    return {net: sorted(names) for net, names in sorted(nets.items()) if len(names) > 1}


def arp_settings(host: Host) -> tuple[str, str]:
    """The live ``arp_ignore`` and ``arp_announce`` (want "1" and "2")."""
    return (
        (host.read_text(ARP / "arp_ignore") or "").strip(),
        (host.read_text(ARP / "arp_announce") or "").strip(),
    )


class _Doctor:
    def __init__(self, host: Host, options: Options) -> None:
        self.host = host
        self.root = host.is_root()
        self.user = options.user or host.sudo_user() or (None if self.root else host.current_user())
        self._config: dict[str, Any] | None = None

    # -- benchhud's config ------------------------------------------------

    def config(self) -> dict[str, Any] | None:
        """benchhud's config, or None when there's no benchhud (or no one to ask as)."""
        if self._config is None:
            self._config = benchhud_config(self.host, self.user)
        return self._config

    # -- services ---------------------------------------------------------

    def service(self, service: Service, report: Report) -> None:
        host = self.host
        name = service.name
        mac = host.is_macos()
        fix = "bench-setup services" if mac else "sudo bench-setup services"
        logs = (
            f"the {service.mac_app} icon in the menu bar" if mac else f"journalctl -u {name} -n 30"
        )
        if mac:
            if service.mac_app is None:
                report.add(OFF, f"{name}: no Mac app (it runs in a container)")
                return
            bundle = host.app_bundle(service.mac_app, self.user)
            if bundle is None:
                report.add(OFF, f"{name}: not installed")
                return
            if not host.process_running(service.mac_app):
                report.add(WARN, f"{name}: the {service.mac_app} app isn't running. Run: {fix}")
                return
            settings = self._mac_settings(service, bundle)
        else:
            if not host.installed(name):
                report.add(OFF, f"{name}: not installed")
                return
            if not host.unit_enabled(name) and not host.unit_active(name):
                if service.dormant:
                    report.add(OFF, f"{name}: installed, off")
                else:
                    report.add(WARN, f"{name}: installed but not turned on. Run: {fix}")
                return
            settings = self._settings(service)
        port = service.default_port
        if settings is not None and settings.get(service.port_key, "").isdigit():
            port = int(settings[service.port_key])
        where = f"127.0.0.1:{port}"

        body = host.healthz(port)
        if body is None:
            hint = (
                ""
                if self.root or mac
                else " (with sudo, doctor reads the real port from its settings)"
            )
            report.add(FAIL, f"{name}: nothing answers on {where}{hint}. See: {logs}")
            return
        if body.get("app") != name:
            other = body.get("app") or "something else"
            report.add(FAIL, f"{name}: {where} is answered by {other}")
            return

        facts = [f"answering on {where}"]
        level = OK

        wide = self._beyond_loopback(port)
        if wide:
            reach = ", ".join(wide)
            if service.dormant:
                report.add(
                    FAIL,
                    f"{name}: listens on {reach}, beyond this machine, "
                    f"and it holds {service.holds}",
                )
                return
            level = WARN
            facts.append(f"listens on {reach}: the LAN can reach it, and it holds {service.holds}")

        if settings is None:
            facts.append(
                "password and token not checked "
                + ("(the app is too old to ask)" if mac else "(needs sudo)")
            )
        else:
            for key, what in ((service.password_key, "password"), (service.token_key, "token")):
                if settings.get(key):
                    facts.append(f"{what} set")
                else:
                    report.add(FAIL, f"{name}: no {what} set. Run: {fix}")
                    return

        wiring_level, wiring = self._wiring(service, port, settings)
        if wiring_level == FAIL:
            report.add(FAIL, f"{name}: {wiring}")
            return
        if wiring:
            facts.append(wiring)
        report.add(level, f"{name}: {', '.join(facts)}")

    def _mac_settings(self, service: Service, bundle: Path) -> dict[str, str] | None:
        """The Mac app's settings.env, or None when its binary is too old to ask."""
        if self.user is None or self.host.app_version(bundle) < service.mac_min:
            return None
        binary = str(bundle / "Contents" / "MacOS" / str(service.mac_app))
        code, out, _ = self.host.run_as_user(self.user, [binary, "paths"])
        for line in out.splitlines() if code == 0 else []:
            key, _, value = line.partition(":")
            if key.strip() == "settings" and value.strip():
                return parse_settings(self.host.read_text(Path(value.strip())) or "")
        return None

    def _settings(self, service: Service) -> dict[str, str] | None:
        """settings.env, or None when it can't be read (it's root's alone)."""
        text = self.host.read_text(service.settings_path)
        if text is None:
            # Root can read it if it's there: no file means nothing is set.
            return {} if self.root else None
        return parse_settings(text)

    def _beyond_loopback(self, port: int) -> list[str]:
        found: list[str] = []
        if self.host.is_macos():
            code, out, _ = self.host.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
            return _beyond_loopback(lsof_addresses(out)) if code == 0 else []
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            text = self.host.read_text(Path(table))
            if text:
                found += listening_addresses(text, port)
        return _beyond_loopback(found)

    def _wiring(
        self, service: Service, port: int, settings: dict[str, str] | None
    ) -> tuple[str, str]:
        config = self.config()
        if config is None:
            return OK, ""
        section = config.get(service.section)
        section = section if isinstance(section, dict) else {}
        url = f"http://127.0.0.1:{port}"
        fix = (
            "Run: bench-setup services"
            if self.host.is_macos()
            else "Run: sudo bench-setup services"
        )
        if not section.get("token"):
            return FAIL, f"is running, but benchhud has no token for it. {fix}"
        if section.get("base_url") != url:
            return (
                FAIL,
                f"benchhud points at {section.get('base_url') or 'nothing'}, not {url}. {fix}",
            )
        if settings is None:
            return OK, "benchhud has its URL and a token (the match wasn't checked)"
        if section["token"] != settings.get(service.token_key):
            return FAIL, f"benchhud's token for it isn't the service's token. {fix}"
        return OK, "benchhud wired"

    # -- host -------------------------------------------------------------

    def loopback(self, report: Report) -> None:
        config = self.config()
        if config is None or self.host.is_macos():
            return
        output = config.get("output")
        output = output if isinstance(output, dict) else {}
        if output.get("enabled") is False:
            report.add(OFF, "virtual camera: benchhud's output is off")
            return
        nodes = output_nodes(config)
        missing = [node for node in nodes if not self.host.exists(Path(node))]
        if not missing:
            report.add(OK, f"virtual camera: {', '.join(nodes)}")
        elif not self.host.exists(LOOPBACK_MODULE):
            report.add(
                WARN,
                f"virtual camera: {', '.join(missing)} missing, and v4l2loopback isn't loaded. "
                "Fix: sudo bench-setup host",
            )
        else:
            report.add(
                WARN,
                f"virtual camera: {', '.join(missing)} missing though v4l2loopback is loaded. "
                "Check /etc/modprobe.d/benchhud-loopback.conf, then: sudo update-initramfs -u",
            )

    def obs_extension(self, report: Report) -> None:
        """macOS: benchhud publishes into OBS's virtual camera, which macOS must allow."""
        if not self.host.is_macos():
            return
        config = self.config()
        output = (config or {}).get("output")
        if isinstance(output, dict) and output.get("enabled") is False:
            report.add(OFF, "virtual camera: benchhud's output is off")
            return
        level, text = obs_extension_state(self.host)
        report.add(level, f"virtual camera: {text}")

    def nodrop(self, report: Report) -> None:
        value = self.host.read_text(NODROP)
        if value is None:
            return  # uvcvideo isn't loaded: no UVC camera has been plugged in
        if value.strip() == "0":
            report.add(OK, "uvcvideo: drops torn frames (nodrop=0)")
        else:
            report.add(
                WARN,
                f"uvcvideo: passes torn frames on (nodrop={value.strip()}), so the thermal image "
                "can tear. Fix: sudo bench-setup host",
            )

    def cameras(self, report: Report) -> None:
        names = self.host.glob("/dev/cam-*")
        if names:
            report.add(OK, f"camera names: {', '.join(names)}")
        elif self.host.read_text(NODROP) is not None:
            report.add(
                WARN,
                "camera names: no /dev/cam-* names, so an OBS source can end up on the wrong "
                "camera. Fix: benchhud udev-rules | sudo tee "
                "/etc/udev/rules.d/70-benchhud-cameras.rules",
            )
        if self.host.exists(_THERMAL_CAM) and not self.host.exists(_THERMAL_RULE):
            report.add(
                WARN,
                f"thermal camera: no {_THERMAL_RULE}, so USB autosuspend can drop it off the bus",
            )

    def network(self, report: Report) -> None:
        shared = shared_subnets(self.host)
        if not shared:
            return
        ignore, announce = arp_settings(self.host)
        for net, names in shared.items():
            cards = " and ".join(names)
            if ignore == "1" and announce == "2":
                report.add(OK, f"network: {cards} share {net}, with the ARP settings in place")
            else:
                report.add(
                    WARN,
                    f"network: {cards} are both on {net} with arp_ignore={ignore or '?'}, "
                    f"arp_announce={announce or '?'}; the box can boot with no IPv4 routes. "
                    "Fix: sudo bench-setup host",
                )


def examine(host: Host, options: Options) -> tuple[Report, Report]:
    """The two reports: services, then host."""
    doctor = _Doctor(host, options)
    services, machine = Report(), Report()
    for service in SERVICES:
        doctor.service(service, services)
    # A Mac has none of the Linux host setup: only OBS's camera extension.
    checks = (
        (doctor.obs_extension,)
        if host.is_macos()
        else (doctor.loopback, doctor.nodrop, doctor.cameras, doctor.network)
    )
    for check in checks:
        check(machine)
    return services, machine


def run(options: Options, host: Host | None = None, ui: UI | None = None) -> int:
    host = host or Host()
    ui = ui or TerminalUI(interactive=False)
    services, machine = examine(host, options)
    for title, report in (("Services", services), ("Host", machine)):
        if not report.lines:
            continue
        ui.say(title)
        for level, text in report.lines:
            ui.say(f"  {level:<5} {text}")
    problems = services.count(FAIL) + machine.count(FAIL)
    warnings = services.count(WARN) + machine.count(WARN)
    if not host.is_root() and not host.is_macos():
        ui.say("Run it with sudo to check passwords, tokens and that benchhud's tokens match.")
    if problems or warnings:
        ui.say(
            f"{problems} problem{'s' * (problems != 1)}, {warnings} warning{'s' * (warnings != 1)}."
        )
    else:
        ui.say("Nothing wrong.")
    return EXIT_FAILED if problems else EXIT_OK
