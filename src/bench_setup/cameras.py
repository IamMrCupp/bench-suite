"""``bench-setup cameras``: names for the bench cameras that don't move.

``/dev/videoN`` numbers are handed out in plug-in order. OBS saves them, so
after the thermal camera drops off USB and comes back, an OBS source can open
the wrong camera, and the HUD then can't open its own. benchhud already knows
how to write the udev rules that fix this (``benchhud udev-rules``); this
command is the guided way to install them:

1. list the cameras that are plugged in,
2. ask which is the scope and which is the thermal, if benchhud doesn't know,
3. install the rules that give each a ``/dev/cam-*`` name,
4. turn USB autosuspend off for the thermal camera,
5. and say what to point OBS at.

The files carry bench-setup's marker, so ``sudo bench-setup host --undo``
removes them with the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench_setup.doctor import benchhud_config
from bench_setup.host import Host
from bench_setup.hostsetup import CAMERA_RULES, MARKER, THERMAL_RULE, marked
from bench_setup.services import EXIT_FAILED, EXIT_OK, EXIT_USAGE, UI, TerminalUI

BY_ID = "/dev/v4l/by-id/usb-*-video-index0"
ROLES = ("scope", "thermal")


@dataclass(frozen=True)
class Options:
    interactive: bool = True
    yes: bool = False
    user: str | None = None


def thermal_rule(vendor: str, product: str) -> str:
    """The udev rule that keeps USB autosuspend off for one camera."""
    return (
        'ACTION=="add", SUBSYSTEM=="usb", '
        f'ATTR{{idVendor}}=="{vendor}", ATTR{{idProduct}}=="{product}", '
        'TEST=="power/control", ATTR{power/control}="on"'
    )


def _rules_only(text: str) -> list[str]:
    """A rules file without its comments, for telling whether two say the same thing."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


class _Failed(Exception):
    pass


class _Cameras:
    def __init__(self, host: Host, ui: UI, options: Options) -> None:
        self.host = host
        self.ui = ui
        self.options = options
        self.user = options.user or host.sudo_user()

    def _ask(self, question: str) -> bool:
        return True if self.options.yes else self.ui.ask_yes_no(question)

    def _roles(self) -> dict[str, str]:
        config = benchhud_config(self.host, self.user) or {}
        panels = config.get("panels")
        panels = panels if isinstance(panels, dict) else {}
        found = {}
        for role in ROLES:
            section: Any = panels.get(role)
            found[role] = section.get("device", "") if isinstance(section, dict) else ""
        return found

    def go(self) -> int:
        host, ui = self.host, self.ui
        self.benchhud = host.benchhud(self.user) or ""
        if not self.benchhud:
            raise _Failed("benchhud isn't installed, and it's what writes the camera rules.")
        if self.user is None or not host.user_exists(self.user):
            raise _Failed(
                "can't tell whose benchhud to ask. Run it with sudo from your own account, "
                "or pass --user NAME."
            )
        cameras = host.glob(BY_ID)
        if not cameras:
            raise _Failed("no USB cameras are plugged in. Plug them all in and run it again.")

        roles = self._roles()
        ui.say("Cameras plugged in:")
        for number, link in enumerate(cameras, 1):
            # uvcvideo names a camera "<product>: <interface>", which on the
            # bench repeats itself ("Cam Link 4K: Cam Link 4K"): keep the product.
            label = (host.camera_label(link) or Path(link).name).split(": ", 1)[0]
            role = next((r for r in ROLES if host.same_device(roles[r], link)), None)
            ui.say(f"  {number}. {label}" + (f"  (benchhud's {role})" if role else ""))
        self._pick_roles(cameras, roles)

        changed = self._camera_rules()
        changed |= self._thermal(self._roles())
        if changed:
            host.run(["udevadm", "control", "--reload"])
            host.run(["udevadm", "trigger"])
            host.run(["udevadm", "settle", "--timeout=10"])
        self._report()
        return EXIT_OK

    # -- which camera is which --------------------------------------------

    def _pick_roles(self, cameras: list[str], roles: dict[str, str]) -> None:
        """Ask for the scope and thermal cameras benchhud doesn't have yet."""
        host, ui = self.host, self.ui
        unset = [
            role
            for role in ROLES
            if not any(host.same_device(roles[role], link) for link in cameras)
        ]
        if not unset or not self.options.interactive:
            for role in unset:
                ui.say(
                    f"benchhud has no {role} camera among these, so none gets the "
                    f"/dev/cam-{role} name."
                )
            return
        if host.benchhud_running(self.user or ""):
            ui.say(
                f"benchhud has no {' or '.join(unset)} camera among these, and it's open, so "
                "its config can't be changed from here. Quit it and run this again to pick."
            )
            return
        for role in unset:
            choice = ui.ask_choice(f"Which one is the {role} camera?", len(cameras))
            if choice is None:
                continue
            code, _, err = host.run_as_user(
                self.user or "",
                [self.benchhud, "config", "set", f"panels.{role}.device", "-"],
                stdin=cameras[choice],
            )
            if code != 0:
                tail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
                raise _Failed(f"benchhud config set failed: {tail}")
            ui.say(f"  benchhud's {role} camera is now number {choice + 1}")

    # -- the rules --------------------------------------------------------

    def _install(self, path: Path, wanted: str, question: str, why: str = "") -> bool:
        """Write ``wanted`` to ``path`` unless it already says the same. True if written.

        ``why`` is said only when there's something to decide.
        """
        host, ui = self.host, self.ui
        current = host.read_text(path)
        if current is not None and _rules_only(current) == _rules_only(wanted):
            ui.say(f"{path}: already in place")
            return False
        if why:
            ui.say(why)
        ui.say(f"  would {'replace' if current is not None else 'write'} {path}:")
        for line in wanted.splitlines():
            ui.say(f"    {line}")
        if not self._ask(question):
            ui.say("  left alone")
            return False
        host.write_file(path, wanted)
        ui.say(f"  wrote {path}")
        return True

    def _camera_rules(self) -> bool:
        code, out, err = self.host.run_as_user(self.user or "", [self.benchhud, "udev-rules"])
        if code != 0 or not out.strip():
            tail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
            raise _Failed(f"benchhud udev-rules failed: {tail}")
        return self._install(
            CAMERA_RULES, f"{MARKER}\n{out.strip()}\n", "Give the cameras these names?"
        )

    def _thermal(self, roles: dict[str, str]) -> bool:
        device = roles["thermal"]
        if not device:
            return False
        ids = self.host.usb_ids(device)
        if ids is None:
            self.ui.say(
                "thermal camera: couldn't read its USB ids, so the autosuspend rule is skipped. "
                "Is it plugged in?"
            )
            return False
        return self._install(
            THERMAL_RULE,
            marked(thermal_rule(*ids)),
            "Keep the thermal camera awake?",
            why=(
                "thermal camera: Linux suspends idle USB devices, and some thermal cameras "
                "don't wake cleanly: they reset and drop off the bus."
            ),
        )

    # -- what to do with them ---------------------------------------------

    def _report(self) -> None:
        ui = self.ui
        names = self.host.glob("/dev/cam-*")
        if not names:
            ui.say("No /dev/cam-* names yet. They appear once the rules are installed.")
            return
        ui.say("Camera names:")
        for name in names:
            ui.say(f"  {name}")
        mine = [n for n in names if n in {"/dev/cam-scope", "/dev/cam-thermal"}]
        rest = [n for n in names if n not in mine]
        ui.say("In OBS:")
        if rest:
            ui.say(
                "  Set each Video Capture Device source to its camera's /dev/cam-* name "
                f"({', '.join(rest)}), not a /dev/videoN."
            )
        if mine:
            ui.say(
                f"  Don't add a source for {' or '.join(mine)}. A camera allows one reader, "
                "and benchhud is it; OBS gets those pictures through the benchhud virtual camera."
            )


def run(options: Options, host: Host | None = None, ui: UI | None = None) -> int:
    host = host or Host()
    ui = ui or TerminalUI(options.interactive)
    if host.is_macos():
        ui.say("bench-setup cameras is for a Linux bench. A Mac needs none of it.")
        return EXIT_OK
    if not host.is_root():
        ui.warn("bench-setup cameras writes udev rules. Run it with sudo.")
        return EXIT_USAGE
    if not options.interactive and not options.yes:
        ui.say("Not asking, and no --yes: this run only reports. Nothing will be changed.")
    try:
        return _Cameras(host, ui, options).go()
    except _Failed as exc:
        ui.warn(f"bench-setup cameras: {exc}")
        return EXIT_FAILED
