"""packaging/apt/pool-changes.py: what the apt publish uploads (bench-suite#51)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "packaging" / "apt" / "pool-changes.py"
spec = importlib.util.spec_from_file_location("pool_changes", SCRIPT)
pool_changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_changes)


def stanza(name: str, version: str, sha: str, arch: str = "amd64") -> str:
    return (
        f"Package: {name}\n"
        f"Version: {version}\n"
        f"Architecture: {arch}\n"
        f"Filename: pool/main/{name}/{name}_{version}_{arch}.deb\n"
        "Size: 1000\n"
        f"SHA256: {sha}\n"
        "Description: a package\n"
        " with a continuation line: Filename: not/this\n"
    )


def tree(root: Path, **arches: str) -> Path:
    for arch, text in arches.items():
        folder = root / "dists" / "stable" / "main" / f"binary-{arch}"
        folder.mkdir(parents=True)
        (folder / "Packages").write_text(text)
    return root


def published(root: Path, **arches: str) -> Path:
    root.mkdir()
    for arch, text in arches.items():
        (root / f"Packages-{arch}").write_text(text)
    return root


def run(repo: Path, live: Path, capsys) -> tuple[int, list[str]]:
    code = pool_changes.main(["pool-changes.py", str(repo), str(live)])
    return code, capsys.readouterr().out.splitlines()


def test_nothing_to_upload_when_the_index_is_the_same(tmp_path, capsys) -> None:
    text = stanza("bench-parts", "0.5.0", "aa") + "\n" + stanza("benchhud", "0.14.7", "bb")
    code, lines = run(
        tree(tmp_path / "new", amd64=text), published(tmp_path / "live", amd64=text), capsys
    )
    assert (code, lines) == (0, [])


def test_a_new_version_is_uploaded(tmp_path, capsys) -> None:
    old = stanza("bench-parts", "0.5.0", "aa")
    new = old + "\n" + stanza("bench-parts", "0.5.1", "cc")
    code, lines = run(
        tree(tmp_path / "new", amd64=new), published(tmp_path / "live", amd64=old), capsys
    )
    assert code == 0
    assert lines == ["new\tpool/main/bench-parts/bench-parts_0.5.1_amd64.deb"]


def test_same_name_and_size_with_different_bytes_is_replaced(tmp_path, capsys) -> None:
    # Both stanzas say Size: 1000. Only the checksum tells them apart.
    code, lines = run(
        tree(tmp_path / "new", amd64=stanza("bench-parts", "0.5.0", "dd")),
        published(tmp_path / "live", amd64=stanza("bench-parts", "0.5.0", "aa")),
        capsys,
    )
    assert code == 0
    assert lines == ["replaced\tpool/main/bench-parts/bench-parts_0.5.0_amd64.deb"]


def test_first_publish_uploads_everything(tmp_path, capsys) -> None:
    text = stanza("bench-parts", "0.5.0", "aa") + "\n" + stanza("benchhud", "0.14.7", "bb")
    code, lines = run(tree(tmp_path / "new", amd64=text), tmp_path / "nothing-here", capsys)
    assert code == 0
    assert len(lines) == 2 and all(line.startswith("new\t") for line in lines)


def test_architectures_are_merged_and_all_packages_listed_once(tmp_path, capsys) -> None:
    shared = stanza("bench-setup", "1.5.6", "ee", arch="all")
    amd = shared + "\n" + stanza("benchhud", "0.14.7", "bb")
    arm = shared + "\n" + stanza("benchhud", "0.14.7", "ff", arch="arm64")
    code, lines = run(
        tree(tmp_path / "new", amd64=amd, arm64=arm),
        published(tmp_path / "live", amd64=amd),
        capsys,
    )
    assert code == 0
    assert lines == ["new\tpool/main/benchhud/benchhud_0.14.7_arm64.deb"]


def test_a_repo_with_no_index_is_an_error(tmp_path, capsys) -> None:
    (tmp_path / "new").mkdir()
    assert pool_changes.main(["pool-changes.py", str(tmp_path / "new"), str(tmp_path)]) == 1
    assert pool_changes.main(["pool-changes.py"]) == 2
