"""Plain ``sudo bench-setup``: every step, in order, asking as it goes.

Services first (they're what the install is for), then the host steps, the
camera names if any cameras are plugged in, what to tell OBS, and doctor last
as the summary. A step that fails doesn't stop the ones after it.
"""

from __future__ import annotations

from bench_setup import cameras, doctor, hostsetup, obs, services
from bench_setup.host import Host
from bench_setup.services import EXIT_FAILED, EXIT_USAGE, SERVICES, UI, TerminalUI


def run(host: Host | None = None, ui: UI | None = None) -> int:
    host = host or Host()
    ui = ui or TerminalUI(interactive=True)
    if host.is_macos():
        return _macos(host, ui)
    if not host.is_root():
        ui.warn(
            "bench-setup sets up system services and files. Run it with sudo, "
            "or see what's there without changing anything: bench-setup doctor"
        )
        return EXIT_USAGE

    failed = False

    def step(title: str) -> None:
        ui.say("")
        ui.say(f"== {title} ==")

    step("Services")
    if any(host.installed(service.name) for service in SERVICES):
        failed |= services.run(services.Options(), host=host, ui=ui) != 0
    else:
        ui.say("None of the bench services are installed here, so there's nothing to turn on.")

    step("Host")
    failed |= hostsetup.run(hostsetup.Options(), host=host, ui=ui) != 0

    step("Cameras")
    if host.benchhud() is None:
        ui.say("benchhud isn't installed here, so there are no camera names to set up.")
    elif not host.glob(cameras.BY_ID):
        ui.say("No USB cameras are plugged in. Plug them all in, then: sudo bench-setup cameras")
    else:
        failed |= cameras.run(cameras.Options(), host=host, ui=ui) != 0

    step("OBS")
    obs.run(obs.Options(), host=host, ui=ui)

    step("Check")
    code = doctor.run(doctor.Options(), host=host, ui=ui)
    return EXIT_FAILED if failed else code


def _macos(host: Host, ui: UI) -> int:
    """A Mac has no host steps: the apps, what to tell OBS, and the check."""
    if host.is_root() and host.sudo_user() is None:
        ui.warn("On a Mac, run bench-setup as yourself, without sudo.")
        return EXIT_USAGE
    ui.say("== Services ==")
    failed = services.run(services.Options(), host=host, ui=ui) != 0
    ui.say("")
    ui.say("== OBS ==")
    obs.run(obs.Options(), host=host, ui=ui)
    ui.say("")
    ui.say("== Check ==")
    code = doctor.run(doctor.Options(), host=host, ui=ui)
    return EXIT_FAILED if failed else code
