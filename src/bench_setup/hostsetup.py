"""``bench-setup host``: the host steps from benchhud's BENCH-SETUP.md, as prompts.

Each step looks first, says what it found, shows the file it would write, asks,
writes, and checks the result. A step that doesn't apply says nothing, and one
that's already done says so and changes nothing, so it's safe to run again.

Every file it writes starts with a marker line. ``--undo`` removes the files
that carry the marker and nothing else: a file you wrote by hand from the
document, even at the same path, is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bench_setup.doctor import (
    ARP,
    LOOPBACK_MODULE,
    NODROP,
    arp_settings,
    benchhud_config,
    output_nodes,
    shared_subnets,
)
from bench_setup.host import Host
from bench_setup.services import EXIT_FAILED, EXIT_OK, EXIT_USAGE, UI, TerminalUI

MARKER = "# Written by bench-setup. Remove with: sudo bench-setup host --undo"

LOOPBACK_OPTIONS = Path("/etc/modprobe.d/benchhud-loopback.conf")
LOOPBACK_LOAD = Path("/etc/modules-load.d/benchhud-loopback.conf")
NODROP_OPTIONS = Path("/etc/modprobe.d/benchhud-uvcvideo.conf")
ARP_SYSCTL = Path("/etc/sysctl.d/60-benchhud-arp.conf")
CAMERA_RULES = Path("/etc/udev/rules.d/70-benchhud-cameras.rules")
THERMAL_RULE = Path("/etc/udev/rules.d/60-benchhud-thermal.rules")
#: Everything bench-setup host and bench-setup cameras can write, and so
#: everything --undo may remove.
OURS = (LOOPBACK_OPTIONS, LOOPBACK_LOAD, NODROP_OPTIONS, ARP_SYSCTL, CAMERA_RULES, THERMAL_RULE)

#: Loopback layouts from BENCH-SETUP.md: how many nodes, their numbers and labels.
LAYOUTS: dict[int, tuple[tuple[int, str], ...]] = {
    1: ((10, "benchhud"),),
    2: ((10, "benchhud"), (11, "OBS-Virtual-Camera")),
    4: (
        (10, "benchhud"),
        (11, "OBS-Virtual-Camera"),
        (12, "benchhud-scope"),
        (13, "benchhud-thermal"),
    ),
}

_ARP_LINES = (
    "net.ipv4.conf.all.arp_ignore = 1",
    "net.ipv4.conf.all.arp_announce = 2",
    "net.ipv4.conf.default.arp_ignore = 1",
    "net.ipv4.conf.default.arp_announce = 2",
)


@dataclass(frozen=True)
class Options:
    interactive: bool = True
    #: Answer yes to every question (what a script passes with --non-interactive).
    yes: bool = False
    undo: bool = False
    #: Loopback nodes: 1, 2 or 4. None asks, or reads it off benchhud's config.
    nodes: int | None = None
    user: str | None = None


def loopback_options(nodes: int) -> str:
    """The ``options v4l2loopback`` line for a layout."""
    layout = LAYOUTS[nodes]
    numbers = ",".join(str(number) for number, _ in layout)
    labels = ",".join(label for _, label in layout)
    caps = ",".join("1" for _ in layout)
    if nodes == 1:
        return f"options v4l2loopback exclusive_caps={caps} card_label={labels} video_nr={numbers}"
    return (
        f"options v4l2loopback devices={nodes} video_nr={numbers} "
        f"card_label={labels} exclusive_caps={caps}"
    )


def marked(*lines: str) -> str:
    return "\n".join((MARKER, *lines)) + "\n"


class _Steps:
    def __init__(self, host: Host, ui: UI, options: Options) -> None:
        self.host = host
        self.ui = ui
        self.options = options
        self.user = options.user or host.sudo_user()
        self.failed = False

    # -- asking and writing -----------------------------------------------

    def _ask(self, question: str) -> bool:
        if self.options.yes:
            return True
        return self.ui.ask_yes_no(question)

    def _show(self, path: Path, text: str) -> None:
        self.ui.say(f"  would write {path}:")
        for line in text.splitlines():
            self.ui.say(f"    {line}")

    def _ours(self, path: Path) -> bool:
        text = self.host.read_text(path)
        return text is not None and text.startswith(MARKER)

    def _foreign(self, directory: str, needle: str) -> str | None:
        """A file in ``directory`` that already sets ``needle`` and isn't one of ours."""
        for name in self.host.glob(f"{directory}/*.conf"):
            path = Path(name)
            text = self.host.read_text(path) or ""
            live = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
            if any(needle in line for line in live) and not text.startswith(MARKER):
                return name
        return None

    def _fail(self, message: str) -> None:
        self.failed = True
        self.ui.warn(message)

    # -- the virtual camera -----------------------------------------------

    def loopback(self) -> None:
        host, ui = self.host, self.ui
        config = benchhud_config(host, self.user)
        wanted = output_nodes(config) if config is not None else ["/dev/video10"]
        missing = [node for node in wanted if not host.exists(Path(node))]
        if not missing:
            ui.say(f"virtual camera: {', '.join(wanted)} already there")
            return

        foreign = self._foreign("/etc/modprobe.d", "options v4l2loopback")
        if foreign:
            ui.say(
                f"virtual camera: {', '.join(missing)} missing, but {foreign} already sets the "
                "module's options, so that file is left alone. If it's right, the copy in the "
                "initramfs is probably stale: sudo update-initramfs -u, then quit OBS and "
                "benchhud and run: sudo modprobe -r v4l2loopback && sudo modprobe v4l2loopback"
            )
            return

        nodes = self._layout(wanted)
        options = marked(loopback_options(nodes))
        load = marked("v4l2loopback")
        ui.say(f"virtual camera: {', '.join(missing)} missing.")
        ui.say(
            "  benchhud sends its picture to OBS through a v4l2loopback node. This sets the "
            "module up to load at boot with the right options."
        )
        self._show(LOOPBACK_OPTIONS, options)
        self._show(LOOPBACK_LOAD, load)
        if self._secure_boot():
            ui.say(
                "  Secure Boot is on. The module is built on this machine and isn't signed by "
                "anyone the firmware knows, so the package asks you for a password and the next "
                "reboot stops at a blue MOK screen: choose Enroll MOK and type that password. "
                "Until then the module won't load. bench-setup can't do that part for you."
            )
        if not self._ask("Set up the virtual camera?"):
            ui.say("  left alone")
            return

        if host.run(["modinfo", "v4l2loopback"])[0] != 0:
            ui.say("  installing v4l2loopback-dkms (it builds the module, which takes a minute)")
            code, _, err = host.run(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "-q",
                    "v4l2loopback-dkms",
                    "v4l2loopback-utils",
                ]
            )
            if code != 0:
                tail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
                self._fail(f"virtual camera: couldn't install v4l2loopback-dkms: {tail}")
                return
        host.write_file(LOOPBACK_OPTIONS, options)
        host.write_file(LOOPBACK_LOAD, load)
        ui.say(f"  wrote {LOOPBACK_OPTIONS} and {LOOPBACK_LOAD}")
        # The module loads from the initramfs at boot, with the initramfs's
        # own copy of the options file: rebuild it or the old copy wins.
        if host.installed("update-initramfs"):
            if host.run(["update-initramfs", "-u"])[0] == 0:
                ui.say("  rebuilt the initramfs, so the options hold after a reboot")
            else:
                self._fail(
                    "virtual camera: update-initramfs -u failed. Run it by hand, or the options "
                    "won't be the ones the module gets at boot."
                )

        if host.exists(LOOPBACK_MODULE):
            ui.say(
                "  v4l2loopback is already loaded with other options, and reloading it under "
                "OBS or benchhud isn't safe. Quit both, then: "
                "sudo modprobe -r v4l2loopback && sudo modprobe v4l2loopback"
            )
            return
        if not self._ask("Load the module now? (No leaves it for the next reboot.)"):
            ui.say("  it loads at the next boot")
            return
        code, _, err = host.run(["modprobe", "v4l2loopback"])
        if code != 0:
            hint = (
                " Secure Boot is refusing the unsigned module: reboot and finish the MOK screen."
                if "rejected" in err
                else ""
            )
            tail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
            self._fail(f"virtual camera: modprobe v4l2loopback failed: {tail}.{hint}")
            return
        first = f"/dev/video{LAYOUTS[nodes][0][0]}"
        if host.exists(Path(first)):
            ui.say(f"  loaded: {first} is there")
        else:
            self._fail(f"virtual camera: the module loaded but {first} didn't appear")

    def _layout(self, wanted: list[str]) -> int:
        if self.options.nodes is not None:
            return self.options.nodes
        if len(wanted) > 1:
            return 4  # benchhud's config already names the scope or thermal feed
        if self.options.yes or not self.options.interactive:
            return 1
        if self.ui.ask_yes_no("  Separate scope and thermal feeds for OBS as well?"):
            return 4
        if self.ui.ask_yes_no("  Will you also use OBS's own Start Virtual Camera?"):
            return 2
        return 1

    def _secure_boot(self) -> bool:
        code, out, _ = self.host.run(["mokutil", "--sb-state"])
        return code == 0 and "enabled" in out.lower()

    # -- torn frames ------------------------------------------------------

    def nodrop(self) -> None:
        host, ui = self.host, self.ui
        value = host.read_text(NODROP)
        if value is None:
            return  # no UVC camera has ever been plugged in
        kept = self._ours(NODROP_OPTIONS) or bool(
            self._foreign("/etc/modprobe.d", "options uvcvideo nodrop=0")
        )
        if value.strip() == "0" and kept:
            ui.say("uvcvideo: already drops torn frames (nodrop=0), and it's set for boot")
            return
        text = marked("options uvcvideo nodrop=0")
        ui.say(
            f"uvcvideo: nodrop={value.strip()}" + ("" if kept else ", and nothing sets it at boot")
        )
        ui.say(
            "  With nodrop on, the camera driver hands over frames that lost part of their "
            "data on USB, and the thermal image tears. Off, it drops them and the last frame "
            "holds a moment longer."
        )
        if not kept:
            self._show(NODROP_OPTIONS, text)
        if not self._ask("Have the driver drop torn frames?"):
            ui.say("  left alone")
            return
        if not kept:
            host.write_file(NODROP_OPTIONS, text)
            ui.say(f"  wrote {NODROP_OPTIONS}")
        if not host.write_value(NODROP, "0") or (host.read_text(NODROP) or "").strip() != "0":
            self._fail(f"uvcvideo: couldn't set {NODROP} to 0")
            return
        ui.say("  nodrop is 0 now. Restart benchhud so the cameras reopen with it.")

    # -- two cards on one subnet ------------------------------------------

    def arp(self) -> None:
        host, ui = self.host, self.ui
        shared = shared_subnets(host)
        if not shared:
            return
        where = "; ".join(f"{' and '.join(names)} on {net}" for net, names in shared.items())
        if arp_settings(host) == ("1", "2"):
            ui.say(f"network: {where}, and the ARP settings are already in place")
            return
        foreign = self._foreign("/etc/sysctl.d", "arp_ignore")
        if foreign:
            ui.say(
                f"network: {where}. {foreign} already sets arp_ignore, so it's left alone, "
                "but it isn't in effect. Apply it with: sudo sysctl --system"
            )
            return
        text = marked(*_ARP_LINES)
        ui.say(f"network: {where}.")
        ui.say(
            "  With two cards on one subnet, each answers ARP for the other's address, "
            "NetworkManager sees a conflict with itself, and the box can come up with no IPv4 "
            "routes. These settings make each card answer only for its own address."
        )
        self._show(ARP_SYSCTL, text)
        if not self._ask("Write it and apply it now?"):
            ui.say("  left alone")
            return
        host.write_file(ARP_SYSCTL, text)
        code, _, err = host.run(["sysctl", "-q", "-p", str(ARP_SYSCTL)])
        if code != 0 or arp_settings(host) != ("1", "2"):
            tail = err.strip().splitlines()[-1] if err.strip() else "the values didn't change"
            self._fail(f"network: wrote {ARP_SYSCTL} but couldn't apply it: {tail}")
            return
        ui.say(
            f"  wrote {ARP_SYSCTL} and applied it. Reconnect both cards to pick it up: "
            "nmcli connection down NAME; nmcli connection up NAME"
        )

    # -- undo -------------------------------------------------------------

    def undo(self) -> None:
        host, ui = self.host, self.ui
        ours = [path for path in OURS if self._ours(path)]
        kept = [path for path in OURS if path not in ours and host.exists(path)]
        for path in kept:
            ui.say(f"{path}: not written by bench-setup, left alone")
        if not ours:
            ui.say("Nothing to undo: bench-setup hasn't written anything here.")
            return
        ui.say("bench-setup wrote these, and would remove them:")
        for path in ours:
            ui.say(f"  {path}")
        if not self._ask("Remove them?"):
            ui.say("left alone")
            return
        for path in ours:
            host.remove(path)
        ui.say(f"removed {len(ours)} file{'s' * (len(ours) != 1)}")
        if (LOOPBACK_OPTIONS in ours or LOOPBACK_LOAD in ours) and host.installed(
            "update-initramfs"
        ):
            if host.run(["update-initramfs", "-u"])[0] == 0:
                ui.say("rebuilt the initramfs without them")
            else:
                self._fail("update-initramfs -u failed. Run it by hand.")
        if CAMERA_RULES in ours or THERMAL_RULE in ours:
            host.run(["udevadm", "control", "--reload"])
            ui.say("reloaded udev; the /dev/cam-* names go when the cameras are next plugged in")
        ui.say(
            "What's running now stays as it is until the next reboot: a loaded module, the "
            f"live nodrop value and the live ARP settings under {ARP} aren't changed back."
        )


def run(options: Options, host: Host | None = None, ui: UI | None = None) -> int:
    host = host or Host()
    ui = ui or TerminalUI(options.interactive)
    if not host.is_root():
        ui.warn("bench-setup host changes system files. Run it with sudo.")
        return EXIT_USAGE
    if options.nodes is not None and options.nodes not in LAYOUTS:
        ui.warn("--nodes is 1, 2 or 4.")
        return EXIT_USAGE
    if not options.interactive and not options.yes:
        ui.say("Not asking, and no --yes: this run only reports. Nothing will be changed.")
    steps = _Steps(host, ui, options)
    if options.undo:
        steps.undo()
    else:
        for step in (steps.loopback, steps.nodrop, steps.arp):
            step()
    return EXIT_FAILED if steps.failed else EXIT_OK
