#!/bin/sh
# Collect the .debs the apt repo carries: every .deb attached to the last
# KEEP releases of each bench repo.
#
#   sh packaging/apt/fetch-debs.sh OUT_DIR
#
# Needs the gh CLI (GH_TOKEN in CI). The repos are public, so any token works.
# A release with no .deb attached is skipped without counting against KEEP.
set -eu

OUT=${1:?usage: fetch-debs.sh OUT_DIR}
KEEP=${KEEP:-5}
OWNER=${OWNER:-IamMrCupp}
REPOS=${REPOS:-"benchhud benchhud-intake bench-parts bench-recon bench-suite"}

mkdir -p "$OUT"
for repo in $REPOS; do
    kept=0
    for tag in $(gh release list -R "$OWNER/$repo" --exclude-drafts --exclude-pre-releases \
        --limit 30 --json tagName -q '.[].tagName'); do
        [ "$kept" -lt "$KEEP" ] || break
        if gh release download "$tag" -R "$OWNER/$repo" -p '*.deb' -D "$OUT" --clobber 2>/dev/null; then
            kept=$((kept + 1))
        fi
    done
    echo "$repo: $kept release(s) with packages"
done

# Release asset names can't hold "~", so GitHub stores benchhud's
# "0.13.0-1~ubuntu-resolute" as "0.13.0-1.ubuntu-resolute". Name every file
# the way apt expects, from the package's own control data.
for deb in "$OUT"/*.deb; do
    want="$OUT/$(dpkg-deb -f "$deb" Package)_$(dpkg-deb -f "$deb" Version)_$(dpkg-deb -f "$deb" Architecture).deb"
    [ "$deb" = "$want" ] || mv "$deb" "$want"
done
ls -l "$OUT"
