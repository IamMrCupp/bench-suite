#!/usr/bin/env python3
"""Which packages in a freshly built repo aren't what's already published.

    python3 packaging/apt/pool-changes.py NEW_REPO PUBLISHED_DIR

NEW_REPO is the tree build-repo.sh made. PUBLISHED_DIR holds the Packages
files of the repo that's live now, one per architecture, under any names; it
may be empty or missing on a first publish.

Prints one line per package to upload: ``new<TAB>pool/...`` for a path the live
index doesn't list, ``replaced<TAB>pool/...`` for a path whose SHA256 changed.
A .deb's name carries its version, but a release can be rebuilt under the same
name, and then name and size don't tell the two files apart (bench-suite#51).
"""

from __future__ import annotations

import sys
from pathlib import Path


def index(text: str) -> dict[str, str]:
    """``Filename -> SHA256`` for every stanza of a Packages file."""
    found: dict[str, str] = {}
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            key, sep, value = line.partition(":")
            if sep and not line[:1].isspace():
                fields[key] = value.strip()
        if "Filename" in fields and "SHA256" in fields:
            found[fields["Filename"]] = fields["SHA256"]
    return found


def read_all(paths: list[Path]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in paths:
        merged.update(index(path.read_text("utf-8")))
    return merged


def changes(new: dict[str, str], published: dict[str, str]) -> list[tuple[str, str]]:
    out = []
    for filename in sorted(new):
        if filename not in published:
            out.append(("new", filename))
        elif published[filename] != new[filename]:
            out.append(("replaced", filename))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    repo, published_dir = Path(argv[1]), Path(argv[2])
    new = read_all(sorted(repo.glob("dists/*/*/binary-*/Packages")))
    if not new:
        print(f"no Packages files under {repo}/dists", file=sys.stderr)
        return 1
    published = read_all(sorted(p for p in published_dir.glob("*") if p.is_file()))
    for kind, filename in changes(new, published):
        print(f"{kind}\t{filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
