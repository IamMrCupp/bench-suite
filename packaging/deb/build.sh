#!/bin/sh
# Build the two .debs this repo ships, into dist/:
#
#   sh packaging/deb/build.sh
#
#   bench-setup_<version>_all.deb   the setup tool (pure Python, standard library only)
#   bench-suite_<version>_all.deb   the metapackage: pulls in benchhud, the services and bench-setup
#
# Needs dpkg-deb, so run it on Debian/Ubuntu (CI does). Nothing is compiled
# and nothing is downloaded: both are Architecture: all.
set -eu

cd "$(dirname "$0")/../.."
VERSION=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' src/bench_setup/__init__.py)
[ -n "$VERSION" ] || { echo "no version in src/bench_setup/__init__.py" >&2; exit 1; }
MAINTAINER="Aaron Cupp <mrcupp@mrcupp.com>"
HOMEPAGE="https://github.com/IamMrCupp/bench-suite"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p dist

doc() {  # doc ROOT PACKAGE: the license and notices every package must carry
    install -d "$1/usr/share/doc/$2"
    install -m 0644 LICENSE "$1/usr/share/doc/$2/copyright"
    install -m 0644 NOTICES "$1/usr/share/doc/$2/NOTICES"
}

# -- bench-setup ----------------------------------------------------------
ROOT="$WORK/bench-setup"
install -d "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/share/bench-setup/bench_setup" \
    "$ROOT/usr/share/bench-suite"
install -m 0644 src/bench_setup/*.py "$ROOT/usr/share/bench-setup/bench_setup/"
install -m 0755 packaging/deb/bench-setup "$ROOT/usr/bin/bench-setup"
# Where `bench-setup services --compose` looks when there's none in the
# current folder.
install -m 0644 compose.yaml "$ROOT/usr/share/bench-suite/compose.yaml"
doc "$ROOT" bench-setup

# python3 >= 3.11: tomllib. No bytecode is shipped or written (the code sits
# outside dist-packages), so there's nothing to clean up on removal.
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: bench-setup
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Depends: python3 (>= 3.11)
Suggests: docker-compose-v2
Section: admin
Priority: optional
Homepage: $HOMEPAGE
Description: Set up the bench services and wire benchhud to them
 Sets one password on benchhud-intake, bench-parts and bench-recon, makes
 each one's API token, starts them, and writes their URLs and tokens into
 benchhud's config. Safe to run again: it only changes what's missing.
 bench-recon stays off unless the owner turns it on.
CONTROL
dpkg-deb --build --root-owner-group "$ROOT" "dist/bench-setup_${VERSION}_all.deb"

# -- bench-suite (metapackage) --------------------------------------------
# The version floors are the first releases bench-setup can drive: benchhud's
# `config set`, and the services' `set-password --stdin`, `new-token --print`,
# 8401-8403 ports and /healthz app name.
ROOT="$WORK/bench-suite"
install -d "$ROOT/DEBIAN"
doc "$ROOT" bench-suite
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: bench-suite
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Depends: benchhud (>= 0.12.0)
Recommends: benchhud-intake (>= 0.3.0), bench-parts (>= 0.4.0), bench-recon (>= 0.3.0), bench-setup (>= $VERSION)
Suggests: obs-studio, v4l2loopback-dkms, v4l2loopback-utils
Section: metapackages
Priority: optional
Homepage: $HOMEPAGE
Description: The repair bench: HUD, intake, parts and recon in one install
 Pulls in benchhud and, by default, benchhud-intake, bench-parts,
 bench-recon and bench-setup. bench-recon is installed switched off.
 .
 After installing, run: sudo bench-setup services
CONTROL
dpkg-deb --build --root-owner-group "$ROOT" "dist/bench-suite_${VERSION}_all.deb"

for deb in dist/bench-setup_"${VERSION}"_all.deb dist/bench-suite_"${VERSION}"_all.deb; do
    echo "== $deb"
    dpkg-deb --info "$deb" | sed -n '/Package:/,$p'
done
dpkg-deb --contents "dist/bench-setup_${VERSION}_all.deb"
