"""``bench-setup obs``: what to point OBS at. Reads, never writes.

OBS scenes are personal, so nothing here builds them. It says which devices
the bench gives OBS, by the names that hold still, and reads the scene
collections OBS already has for the two mistakes that break the bench: a source
saved as ``/dev/videoN``, and a source on a camera benchhud needs for itself.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench_setup.doctor import benchhud_config, output_nodes
from bench_setup.host import Host
from bench_setup.services import EXIT_OK, UI, TerminalUI

SCENES = Path(".config/obs-studio/basic/scenes")
#: The names benchhud keeps for itself: one reader per camera, and it's the reader.
RESERVED = ("/dev/cam-scope", "/dev/cam-thermal")
_RAW_NODE = re.compile(r"^/dev/video\d+$")


@dataclass(frozen=True)
class Options:
    user: str | None = None


@dataclass(frozen=True)
class Finding:
    collection: str
    source: str
    device: str
    problem: str  # "raw" (a /dev/videoN that can move) or "reserved" (benchhud's camera)


def capture_sources(collection: dict[str, Any]) -> list[tuple[str, str]]:
    """``(source name, device)`` for every V4L2 capture source in a scene collection."""
    found = []
    for source in collection.get("sources", []):
        if not isinstance(source, dict) or source.get("id") != "v4l2_input":
            continue
        settings = source.get("settings")
        device = settings.get("device_id", "") if isinstance(settings, dict) else ""
        if isinstance(device, str) and device:
            found.append((str(source.get("name", "?")), device))
    return found


def check_sources(
    name: str,
    sources: list[tuple[str, str]],
    outputs: list[str],
    reserved: list[str],
) -> list[Finding]:
    """What's wrong with a collection's capture sources.

    ``outputs`` are benchhud's loopback nodes: those are pinned by number and
    are meant to be opened as ``/dev/videoN``. ``reserved`` are the real paths
    of the scope and thermal cameras.
    """
    findings = []
    for source, device in sources:
        if device in outputs:
            continue
        if os.path.realpath(device) in reserved or device in RESERVED:
            findings.append(Finding(name, source, device, "reserved"))
        elif _RAW_NODE.match(device):
            findings.append(Finding(name, source, device, "raw"))
    return findings


def run(options: Options, host: Host | None = None, ui: UI | None = None) -> int:
    host = host or Host()
    ui = ui or TerminalUI(interactive=False)
    user = options.user or host.sudo_user() or host.current_user()
    config = benchhud_config(host, user)
    outputs = output_nodes(config) if config is not None else ["/dev/video10"]

    ui.say("benchhud's picture")
    labels = ("the whole HUD", "the scope alone", "the thermal alone")
    output = (config or {}).get("output")
    output = output if isinstance(output, dict) else {}
    slots = [
        (outputs[0], labels[0]),
        *(
            (output[key], label)
            for key, label in zip(("scope_device", "thermal_device"), labels[1:], strict=True)
            if output.get(key)
        ),
    ]
    for node, what in slots:
        state = "" if host.exists(Path(node)) else "  (missing: sudo bench-setup host)"
        ui.say(f"  {node}: {what}{state}")
    ui.say(
        "  Add each to OBS as a Video Capture Device (V4L2). These are the only devices "
        "to pick by /dev/videoN: their numbers are pinned."
    )

    names = host.glob("/dev/cam-*")
    others = [name for name in names if name not in RESERVED]
    ui.say("The other cameras")
    if not names:
        ui.say("  No /dev/cam-* names yet. Set them up with: sudo bench-setup cameras")
    else:
        for name in others:
            ui.say(f"  {name}")
        if others:
            ui.say(
                "  Use these names in OBS, not /dev/videoN: a number can move to another camera."
            )
        mine = [name for name in names if name in RESERVED]
        if mine:
            ui.say(
                f"  Not {' or '.join(mine)}: a camera allows one reader and benchhud is it. "
                "OBS gets those through benchhud's picture."
            )

    findings = _scan(host, user, outputs)
    if findings:
        ui.say("In your OBS scenes")
        for found in findings:
            if found.problem == "raw":
                ui.say(
                    f"  {found.collection}: source '{found.source}' is saved as {found.device}, "
                    "which can become another camera. Give it the camera's /dev/cam-* name."
                )
            else:
                ui.say(
                    f"  {found.collection}: source '{found.source}' opens {found.device}, "
                    "which benchhud needs for itself. Remove it and use benchhud's picture."
                )
        ui.say(
            "  If OBS's device list doesn't offer a /dev/cam-* name, quit OBS and change "
            "device_id for that source in the scene collection file."
        )
    return EXIT_OK


def _scan(host: Host, user: str, outputs: list[str]) -> list[Finding]:
    home = host.home_of(user)
    if home is None:
        return []
    reserved = [os.path.realpath(name) for name in RESERVED if host.exists(Path(name))]
    findings: list[Finding] = []
    for name in host.glob(str(home / SCENES / "*.json")):
        text = host.read_text(Path(name))
        try:
            collection = json.loads(text or "")
        except ValueError:
            continue
        if isinstance(collection, dict):
            label = str(collection.get("name") or Path(name).stem)
            findings += check_sources(label, capture_sources(collection), outputs, reserved)
    return findings
