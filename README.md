# bench-suite

One install for the repair bench. **bench-suite** sets up the four bench apps on one machine and wires them to each other, so a fresh box goes from nothing to a working bench without hand-copying URLs and tokens between configs.

> **Status: installable on Ubuntu 26.04, amd64.** The signed apt repo is live at `apt.yolorepair.com`, and CI installs from it on a clean machine. On a Mac, a [Homebrew tap](https://github.com/IamMrCupp/homebrew-bench) installs the three apps and `bench-setup`, which wires them together, and CI does the same on a clean Mac.

## What it puts together

| App | What it does | Where it runs |
|---|---|---|
| [benchhud](https://github.com/IamMrCupp/benchhud) | The on-screen command center at the bench — cameras, thermal, scope, the stream-safe overlays | Desktop app |
| [benchhud-intake](https://github.com/IamMrCupp/benchhud-intake) | Work coming in — customers, devices, tickets | Service, `127.0.0.1:8401` |
| [bench-parts](https://github.com/IamMrCupp/bench-parts) | Stock on the shelf, consumed against a job | Service, `127.0.0.1:8402` |
| [bench-recon](https://github.com/IamMrCupp/bench-recon) | Pentest engagements and targets. Installed, but **off until you turn it on** | Service, `127.0.0.1:8403` |

The services listen on loopback only. benchhud talks to them on the same box; nothing is reachable from the LAN unless you put it there.

## Install

```sh
# The repo's signing key, then the repo itself.
sudo curl -fsSLo /usr/share/keyrings/bench-archive-keyring.gpg https://apt.yolorepair.com/bench-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/bench-archive-keyring.gpg] https://apt.yolorepair.com stable main" \
  | sudo tee /etc/apt/sources.list.d/bench.list

sudo apt update
sudo apt install bench-suite
sudo bench-setup
```

The key's fingerprint is `3826 D3D3 D136 48B5 336F  CA37 F099 3D55 EB7D F44C` — check it with `gpg --show-keys /usr/share/keyrings/bench-archive-keyring.gpg` if you like to know what you're trusting.

`bench-suite` is a metapackage: it pulls in benchhud and recommends the three services and `bench-setup`. Plain `sudo bench-setup` then walks every step in order, asking as it goes — the services, [the host](#the-host-steps), [the camera names](#camera-names), [what to tell OBS](#what-to-tell-obs), and [a check](#checking-on-it) at the end. Each step is also a command of its own. The services step, `sudo bench-setup services`:

- asks for **one** password and sets it on every service,
- mints each service's API token and writes it into benchhud's config, with the URLs,
- asks whether to turn bench-recon on,
- and checks every service answers before it calls the job done.

Run it again any time — it only changes what's missing. A service that already has a token keeps it, so a re-run never cuts off whatever else is using it. Upgrades are plain `apt upgrade`.

From a script, nothing prompts and the password comes in on stdin — never on the command line, where `ps` would show it:

```sh
printf '%s\n' "$BENCH_PASSWORD" | sudo bench-setup services --non-interactive
```

bench-recon stays off there too unless you pass `--recon`. Quit benchhud before running it: benchhud saves its config on the way out and would write its old copy over the new one, so `bench-setup` refuses to wire it while it's open.

### Checking on it

```sh
sudo bench-setup doctor
```

`doctor` looks and reports; it changes nothing. For each service: is it on, does it answer as itself, does it listen only on this machine, are the password and token set, and does benchhud hold the matching token. For the host: the things benchhud's [BENCH-SETUP.md](https://github.com/IamMrCupp/benchhud/blob/main/docs/BENCH-SETUP.md) has you set up by hand — the virtual camera nodes, `uvcvideo`'s `nodrop`, the `/dev/cam-*` names, two network cards on one subnet. It never prints a secret, only "set" or not. Without `sudo` it still runs, and says what it couldn't check. It exits 1 if something is broken, so a script can lean on it. It checks the installed packages, not the containers.

### The host steps

```sh
sudo bench-setup host
```

Five things benchhud's [BENCH-SETUP.md](https://github.com/IamMrCupp/benchhud/blob/main/docs/BENCH-SETUP.md) has you do by hand, as prompts:

- **The virtual camera.** The `v4l2loopback` node benchhud sends its picture to OBS through: the options file, load-at-boot, and the initramfs rebuild that makes the options stick. One node, two (if you also use OBS's own virtual camera) or four (separate scope and thermal feeds). Loading the module is its own question — say no and it waits for the next reboot. With Secure Boot on, it tells you about the MOK screen you'll meet; it can't do that part for you.
- **Torn frames.** `uvcvideo nodrop=0`, so a thermal frame that lost data on USB is dropped instead of shown torn.
- **Two network cards on one subnet.** The ARP settings that stop a wired-plus-Wi-Fi bench booting with no IPv4 routes. Only offered when that's the bench you have.
- **Your account's groups.** `dialout`, `video` and `plugdev`, for the serial instruments, the cameras and USB. It adds the ones you're missing; you log out and back in.
- **Resetting a hung camera.** Only offered when `uhubctl` is installed and finds a hub that can switch its ports. It writes one sudoers line letting your account run `uhubctl` as root without a password, which is what lets benchhud power-cycle a camera that stays gone. The file is checked with `visudo` before it's written and again after, and taken back out if sudo objects. You still tell benchhud which port, with `reset_port`.

Each step looks first, shows you the file it would write, and asks. A step that doesn't apply says nothing; one that's already done says so. If you've already set something up by hand, your file is left alone.

`sudo bench-setup host --undo` removes the files `bench-setup` wrote — they carry a marker line — and nothing else. Group memberships stay. From a script, `--non-interactive --yes` answers yes throughout, and without `--yes` it only reports.

### Camera names

```sh
sudo bench-setup cameras
```

`/dev/videoN` numbers follow plug-in order, and OBS saves them. When the thermal camera drops off USB and comes back, an OBS source can open the wrong camera — and then the HUD can't open its own. Run this with every camera plugged in:

- it lists them, and asks which is the scope and which the thermal if benchhud doesn't know yet,
- installs the udev rules that give each a `/dev/cam-*` name (`/dev/cam-scope`, `/dev/cam-thermal`, the rest after their model),
- keeps the thermal camera out of USB autosuspend, which is what makes some of them drop off the bus,
- and tells you which names to give OBS, and which cameras OBS shouldn't open at all.

Run it again after adding a camera. `sudo bench-setup host --undo` removes these rules along with the rest.

### What to tell OBS

```sh
bench-setup obs
```

Scenes are yours, so nothing here builds them. This lists the devices the bench gives OBS by names that hold still — benchhud's picture, and each other camera's `/dev/cam-*` name — and reads the scene collections OBS already has for the two mistakes that break a bench: a source saved as `/dev/videoN`, and a source on the scope or thermal camera, which benchhud needs for itself. It never edits a scene.

### In containers instead

If you'd rather not install the service packages, the three services run from the [`compose.yaml`](compose.yaml) in this repo, and `bench-setup` does the same job through `docker compose`:

```sh
bench-setup services --compose            # ./compose.yaml, or name one
bench-setup services --compose --recon    # ...and turn bench-recon on
```

No `sudo` — it only needs to be able to run Docker. Every port is published on `127.0.0.1` only. bench-recon sits behind a compose profile, so a plain `docker compose up -d` never starts it, and `bench-setup` won't set it up if you've edited the file to publish it beyond this machine. The password and tokens live in each service's volume, not in the compose file or a `.env`.

### On a Mac

There's no apt and no systemd on a Mac, and none of the host steps apply: benchhud's picture goes into OBS's own virtual camera. What's left is the wiring, and `bench-setup` does it against the apps themselves:

```sh
brew tap iammrcupp/bench
brew trust --tap iammrcupp/bench   # Homebrew asks before a tap's casks may pull each other in
brew install --cask bench-suite    # benchhud, both menu-bar apps, bench-setup
bench-setup                        # no sudo: the apps are yours
```

The [tap](https://github.com/IamMrCupp/homebrew-bench) carries all three apps — signed with a Developer ID and notarized, Apple silicon only — and `bench-setup`. `brew upgrade` keeps them current. The `bench-suite` cask has nothing of its own; it pulls in the rest. Each piece also installs by name (`brew install --cask benchhud`, `benchhud-intake`, `bench-parts`; `brew install bench-setup`), and that needs no `brew trust`.

- **benchhud-intake and bench-parts** are menu-bar apps. `bench-setup services` sets the password and makes the token through each app's own binary, opens the app (or quits and reopens one that was running with old settings), and writes the URLs and tokens into benchhud's config.
- **A freshly installed app has to be opened by you once.** macOS asks before the first launch of anything downloaded, and only you can answer. `bench-setup` sets the password and token, then tells you to open the app from Applications and run it again; the second run wires it up.
- **bench-recon** has no Mac app. It runs in a container, off until you turn it on: `bench-setup services --compose --recon`.
- **`bench-setup doctor`** checks the apps the same way, and that macOS has OBS's camera extension turned on — the step people miss.
- `bench-setup host` and `bench-setup cameras` say they're for Linux and do nothing.

It needs app versions whose binaries take commands: benchhud-intake 0.4.0, bench-parts 0.5.0 and benchhud 0.14.4 or newer. With an older one it says so and leaves that app alone. Windows is still to come.

## License

[AGPL-3.0-or-later](LICENSE), like the rest of the bench apps — open source, with commercial licences for anyone the AGPL doesn't fit. See [CONTRIBUTING](CONTRIBUTING.md) before sending a change.
