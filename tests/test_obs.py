"""``bench-setup obs`` and the plain ``bench-setup`` guided run (bench-suite#26)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench_setup import guided, obs
from bench_setup.cli import main
from bench_setup.obs import Finding, capture_sources, check_sources
from bench_setup.services import EXIT_OK, EXIT_USAGE
from test_cameras import CamHost, Picker
from test_services import FakeUI

HOME = Path("/home/aaron")
SCENE = str(HOME / ".config/obs-studio/basic/scenes/Bench.json")


def collection(*sources: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "name": "Bench",
        "sources": [
            {"name": name, "id": kind, "settings": {"device_id": device}}
            for name, kind, device in sources
        ],
    }


class ObsHost(CamHost):
    def __init__(self) -> None:
        super().__init__()
        self.paths |= {"/dev/video10"}
        self.cams = ["/dev/cam-c922-pro-stream-webcam", "/dev/cam-scope", "/dev/cam-thermal"]

    def home_of(self, user: str) -> Path | None:
        return HOME if user == "aaron" else None


def obs_run(host: ObsHost) -> FakeUI:
    ui = FakeUI()
    before = dict(host.files)
    assert obs.run(obs.Options(), host=host, ui=ui) == EXIT_OK  # type: ignore[arg-type]
    assert host.files == before and not host.written, "obs only reads"
    return ui


def test_capture_sources_reads_only_v4l2_inputs() -> None:
    scene = collection(
        ("Webcam", "v4l2_input", "/dev/video4"),
        ("Desktop", "xshm_input", ""),
        ("HUD", "v4l2_input", "/dev/video10"),
    )
    scene["sources"].append("not a source")
    scene["sources"].append({"name": "Empty", "id": "v4l2_input", "settings": {}})
    assert capture_sources(scene) == [("Webcam", "/dev/video4"), ("HUD", "/dev/video10")]
    assert capture_sources({}) == []


def test_check_sources() -> None:
    sources = [
        ("HUD", "/dev/video10"),
        ("Webcam", "/dev/video4"),
        ("Overhead", "/dev/cam-c922-pro-stream-webcam"),
        ("Thermal", "/dev/cam-thermal"),
    ]
    assert check_sources("Bench", sources, ["/dev/video10"], []) == [
        Finding("Bench", "Webcam", "/dev/video4", "raw"),
        Finding("Bench", "Thermal", "/dev/cam-thermal", "reserved"),
    ]


def test_it_lists_what_to_give_obs() -> None:
    host = ObsHost()
    host.config["output"]["thermal_device"] = "/dev/video13"
    ui = obs_run(host)

    assert "  /dev/video10: the whole HUD" in ui.out
    assert "  /dev/video13: the thermal alone  (missing: sudo bench-setup host)" in ui.out
    assert "  /dev/cam-c922-pro-stream-webcam" in ui.out
    assert "  /dev/cam-scope" not in ui.out
    assert any("Not /dev/cam-scope or /dev/cam-thermal" in line for line in ui.out)
    assert "In your OBS scenes" not in ui.out


def test_no_camera_names_points_at_the_cameras_command() -> None:
    host = ObsHost()
    host.cams = []
    ui = obs_run(host)
    assert "  No /dev/cam-* names yet. Set them up with: sudo bench-setup cameras" in ui.out


def test_it_finds_the_two_mistakes_in_a_scene_collection() -> None:
    host = ObsHost()
    host.files[SCENE] = json.dumps(
        collection(
            ("HUD", "v4l2_input", "/dev/video10"),
            ("Webcam", "v4l2_input", "/dev/video4"),
            ("Thermal", "v4l2_input", "/dev/cam-thermal"),
            ("Overhead", "v4l2_input", "/dev/cam-c922-pro-stream-webcam"),
        )
    )
    host.files[str(HOME / ".config/obs-studio/basic/scenes/broken.json")] = "{not json"
    ui = obs_run(host)

    assert "In your OBS scenes" in ui.out
    assert any("Bench: source 'Webcam' is saved as /dev/video4" in line for line in ui.out)
    assert any("Bench: source 'Thermal' opens /dev/cam-thermal" in line for line in ui.out)
    assert not any("'HUD'" in line or "'Overhead'" in line for line in ui.out)


def test_cli_runs_obs(monkeypatch) -> None:
    seen: list[obs.Options] = []
    monkeypatch.setattr(obs, "run", lambda options: seen.append(options) or 0)
    assert main(["obs", "--user", "aaron"]) == 0
    assert seen[-1] == obs.Options(user="aaron")


# -- plain `bench-setup` ------------------------------------------------------


def test_guided_run_needs_root_and_points_at_doctor() -> None:
    host = ObsHost()
    host.root = False
    ui = FakeUI()
    assert guided.run(host=host, ui=ui) == EXIT_USAGE  # type: ignore[arg-type]
    assert "bench-setup doctor" in ui.text
    assert not host.written


def test_guided_run_walks_every_step_in_order() -> None:
    host = ObsHost()
    ui = Picker({}, yes=True)
    code = guided.run(host=host, ui=ui)  # type: ignore[arg-type]

    assert code == EXIT_OK, ui.text
    titles = [line for line in ui.out if line.startswith("== ")]
    assert titles == ["== Services ==", "== Host ==", "== Cameras ==", "== OBS ==", "== Check =="]
    assert "/etc/udev/rules.d/70-benchhud-cameras.rules" in host.written
    assert "/etc/modprobe.d/benchhud-uvcvideo.conf" in host.written
    assert ui.out[-1] in {"Nothing wrong.", "0 problems, 0 warnings."}


def test_guided_run_skips_cameras_when_none_are_plugged_in() -> None:
    host = ObsHost()
    host.links_by_id = []
    ui = Picker({}, yes=False)
    code = guided.run(host=host, ui=ui)  # type: ignore[arg-type]

    assert code == EXIT_OK, ui.text
    assert any("No USB cameras are plugged in" in line for line in ui.out)
    assert not ui.err


def test_cli_with_no_command_is_the_guided_run(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(guided, "run", lambda: called.append(True) or 0)
    assert main([]) == 0
    assert called == [True]
