#!/bin/sh
# Build the signed apt repository tree from a folder of .debs.
#
#   sh packaging/apt/build-repo.sh DEBS_DIR REPO_DIR KEY_ID
#
#   REPO_DIR/pool/main/<package>/<file>.deb
#   REPO_DIR/dists/stable/main/binary-<arch>/Packages{,.gz}
#   REPO_DIR/dists/stable/{Release,Release.gpg,InRelease}
#   REPO_DIR/bench-archive-keyring.gpg     the public key, for signed-by=
#
# KEY_ID names a secret key already in the gpg keyring; CI imports it from a
# secret first. Needs apt-ftparchive (apt-utils) and gpg.
set -eu

DEBS=${1:?usage: build-repo.sh DEBS_DIR REPO_DIR KEY_ID}
REPO=${2:?usage: build-repo.sh DEBS_DIR REPO_DIR KEY_ID}
KEY=${3:?usage: build-repo.sh DEBS_DIR REPO_DIR KEY_ID}
SUITE=stable
COMPONENT=main
ARCHES="amd64 arm64"

rm -rf "$REPO"
mkdir -p "$REPO"
REPO=$(cd "$REPO" && pwd)
for deb in "$DEBS"/*.deb; do
    package=$(dpkg-deb -f "$deb" Package)
    install -D -m 0644 "$deb" "$REPO/pool/$COMPONENT/$package/$(basename "$deb")"
done

cd "$REPO"
for arch in $ARCHES; do
    dir="dists/$SUITE/$COMPONENT/binary-$arch"
    mkdir -p "$dir"
    # --arch keeps this architecture's packages plus the "all" ones.
    apt-ftparchive --arch "$arch" packages pool > "$dir/Packages"
    gzip -9 -k -n "$dir/Packages"
    echo "$arch: $(grep -c '^Package:' "$dir/Packages") package version(s)"
done

apt-ftparchive \
    -o "APT::FTPArchive::Release::Origin=bench" \
    -o "APT::FTPArchive::Release::Label=bench" \
    -o "APT::FTPArchive::Release::Suite=$SUITE" \
    -o "APT::FTPArchive::Release::Codename=$SUITE" \
    -o "APT::FTPArchive::Release::Architectures=$ARCHES" \
    -o "APT::FTPArchive::Release::Components=$COMPONENT" \
    -o "APT::FTPArchive::Release::Description=The repair bench apps" \
    release "dists/$SUITE" > "$REPO.Release"
mv "$REPO.Release" "dists/$SUITE/Release"

gpg --batch --yes --local-user "$KEY" --clearsign -o "dists/$SUITE/InRelease" "dists/$SUITE/Release"
gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o "dists/$SUITE/Release.gpg" "dists/$SUITE/Release"
gpg --batch --yes --export "$KEY" > bench-archive-keyring.gpg

find . -type f | sort
