"""Site generation: the channel feeds, the archive index, and removal.

Network calls are stubbed with the vendored wheel, so these run offline and
assert on the generator's own decisions rather than on PyPI's current state.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_feed  # noqa: E402
from generate_feed import ArchiveRecord, collect_archives, generate, render_archives  # noqa: E402
from marketplace_lib import Lock, PyPIRelease  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "haybale_visiongraph-0.0.38-py3-none-any.whl"
DIST = "haybale-visiongraph"
VERSION = "0.0.38"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with one member, and PyPI replaced by the vendored wheel."""
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / f"{DIST}.toml").write_text(f'name = "{DIST}"\n', encoding="utf-8")

    (tmp_path / "locks").mkdir()
    for channel in ("stable", "latest"):
        lock = Lock(
            channel=channel,
            exclude_newer="2026-08-29T00:00:00Z",
            framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
            pins={DIST: VERSION},
            generated="2026-08-29T00:00:00Z",
        )
        (tmp_path / "locks" / f"{channel}.toml").write_text(lock.to_toml(), encoding="utf-8")

    release = PyPIRelease(
        name=DIST,
        version=VERSION,
        wheel_url="https://pypi.invalid/w.whl",
        yanked=False,
        yanked_reason="",
        uploaded=None,
    )
    monkeypatch.setattr(generate_feed, "fetch_release", lambda name, version: release)
    monkeypatch.setattr(generate_feed, "fetch_all_releases", lambda name: {VERSION: release})
    monkeypatch.setattr(generate_feed, "newest_version", lambda releases, **kw: VERSION)
    # "Downloading" is a copy of the vendored wheel — no network, same bytes.
    monkeypatch.setattr(
        generate_feed, "download_wheel", lambda url, dest: (dest.write_bytes(FIXTURE.read_bytes()), dest)[1]
    )
    return tmp_path


def rows_of(path: Path) -> list[dict]:
    return tomllib.loads(path.read_text(encoding="utf-8")).get("haybales", [])


def test_generates_every_expected_artifact(repo, tmp_path):
    out = tmp_path / "_site"
    generate(repo_root=repo, out_dir=out, channels=("edge", "latest", "stable"), curation_tag="2026.08.29")

    for expected in (
        "index.html",
        "archives.html",
        ".nojekyll",
        "edge/marketplace.toml",
        "latest/marketplace.toml",
        "stable/marketplace.toml",
        "stable/2026.08.29/marketplace.toml",
        "latest/2026.08.29/marketplace.toml",
    ):
        assert (out / expected).is_file(), f"missing {expected}"


def test_edge_is_never_archived(repo, tmp_path):
    """`edge` specs float, so an immutable edge archive is a promise it cannot keep."""
    out = tmp_path / "_site"
    generate(repo_root=repo, out_dir=out, channels=("edge",), curation_tag="2026.08.29")
    assert not (out / "edge" / "2026.08.29").exists()


def test_install_spec_per_channel(repo, tmp_path):
    out = tmp_path / "_site"
    generate(repo_root=repo, out_dir=out, channels=("edge", "latest", "stable"))

    for channel in ("stable", "latest"):
        row = rows_of(out / channel / "marketplace.toml")[0]
        assert row["install_spec"] == f"{DIST}=={VERSION}"
        assert row["version"] == VERSION

    edge = rows_of(out / "edge" / "marketplace.toml")[0]
    assert edge["install_spec"] == DIST
    assert edge["version"] == VERSION  # still advertised, or no update is ever offered


def test_deleting_a_registry_entry_removes_the_row_from_every_channel(repo, tmp_path):
    """The acceptance check: removal is `git rm`, and it must reach all three."""
    out = tmp_path / "_site"
    generate(repo_root=repo, out_dir=out, channels=("edge", "latest", "stable"))
    for channel in ("edge", "latest", "stable"):
        assert len(rows_of(out / channel / "marketplace.toml")) == 1

    (repo / "registry" / f"{DIST}.toml").unlink()
    (repo / "registry" / "haybale-other.toml").write_text('name = "haybale-other"\n', encoding="utf-8")

    generate(repo_root=repo, out_dir=out, channels=("edge", "latest", "stable"))
    for channel in ("edge", "latest", "stable"):
        names = [row["name"] for row in rows_of(out / channel / "marketplace.toml")]
        assert DIST not in names, f"{channel} still carries the removed library"


def test_a_yanked_pin_is_not_published(repo, tmp_path, monkeypatch):
    yanked = PyPIRelease(
        name=DIST,
        version=VERSION,
        wheel_url="https://pypi.invalid/w.whl",
        yanked=True,
        yanked_reason="broken on macOS",
        uploaded=None,
    )
    monkeypatch.setattr(generate_feed, "fetch_release", lambda name, version: yanked)

    out = tmp_path / "_site"
    results = generate(repo_root=repo, out_dir=out, channels=("stable",))
    assert rows_of(out / "stable" / "marketplace.toml") == []
    assert any("yanked" in reason for _, reason in results["stable"].failures)


def test_a_library_not_in_the_lock_is_skipped_not_failed(repo, tmp_path):
    """A library can be in the registry and legitimately not in `stable`."""
    (repo / "registry" / "haybale-absent.toml").write_text('name = "haybale-absent"\n', encoding="utf-8")
    out = tmp_path / "_site"
    results = generate(repo_root=repo, out_dir=out, channels=("stable",))
    assert results["stable"].failures == []
    assert [row["name"] for row in rows_of(out / "stable" / "marketplace.toml")] == [DIST]


# ─────────────────────────────────────────────────────────────────────────────
# The archive index
# ─────────────────────────────────────────────────────────────────────────────


def test_archives_are_discovered_from_what_is_on_disk(repo, tmp_path):
    """Built by scanning the published tree, so it cannot drift from what is served."""
    out = tmp_path / "_site"
    generate(repo_root=repo, out_dir=out, channels=("stable", "latest"), curation_tag="2026.08.29")
    generate(repo_root=repo, out_dir=out, channels=("stable", "latest"), curation_tag="2026.09.15")

    records = collect_archives(out)
    tags = {r.tag for r in records}
    assert tags == {"2026.08.29", "2026.09.15"}
    # Each carries what makes one archive choosable over another.
    for record in records:
        assert record.library_count == 1
        assert record.framework == "0.1.2"
        assert record.generated


def test_the_archive_index_lists_every_archive(repo, tmp_path):
    out = tmp_path / "_site"
    generate(repo_root=repo, out_dir=out, channels=("stable",), curation_tag="2026.08.29")
    html = (out / "archives.html").read_text(encoding="utf-8")
    assert "2026.08.29" in html
    assert "stable/2026.08.29/marketplace.toml" in html


def test_the_archive_index_exists_even_with_no_archives():
    """GitHub Pages serves no directory listings, so the page must always exist."""
    html = render_archives([])
    assert "No archives yet" in html
    assert "<title>" in html


def test_the_archive_index_renders_a_record():
    html = render_archives(
        [ArchiveRecord(tag="2026.08.29", channel="stable", generated="2026-08-29", framework="0.1.2", library_count=4)]
    )
    for expected in ("2026.08.29", "stable", "2026-08-29", "0.1.2", ">4<"):
        assert expected in html
