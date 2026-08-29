"""Row building and feed emission, against a vendored wheel — no network.

The fixture is a real published wheel rather than a synthetic one, because
every check here is about reading a real artifact correctly.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from emit import FIELD_ORDER, _format_value, emit_feed, emit_row  # noqa: E402
from haywire.core.library.haybale import Haybale  # noqa: E402
from haywire.core.marketstall.parsing import parse_marketstall_body  # noqa: E402
from marketplace_lib import (  # noqa: E402
    MarketplaceError,
    build_row,
    read_registry,
    read_wheel_metadata,
    verify_row_version,
)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "haybale_visiongraph-0.0.38-py3-none-any.whl"
DIST = "haybale-visiongraph"
VERSION = "0.0.38"


@pytest.fixture(scope="module")
def metadata():
    return read_wheel_metadata(FIXTURE, DIST)


# ─────────────────────────────────────────────────────────────────────────────
# Reading the wheel
# ─────────────────────────────────────────────────────────────────────────────


def test_reads_haybale_toml_out_of_the_wheel(metadata):
    assert metadata.haybale["name"] == DIST
    assert metadata.haybale["version"] == VERSION
    assert metadata.haybale["label"] == "Visiongraph"
    assert metadata.haybale["linked_libraries"] == ["haybale_core"]


def test_reads_the_entry_point_group(metadata):
    assert metadata.declares_library_entry_point
    assert metadata.entry_points["haywire.libraries"] == {"visiongraph": "haybale_visiongraph:Library"}


def test_require_is_projected_from_the_wheels_own_dependency(metadata):
    # Not authored: read off Requires-Dist through the framework's own parser.
    assert "haywire-core>=0.1.0" in metadata.requires_dist
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="stable")
    assert row.require == "haywire-core>=0.1.0"


def test_wheel_without_a_haybale_toml_is_rejected(tmp_path):
    import zipfile

    wheel = tmp_path / "haybale_fake-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("haybale_fake/__init__.py", "")
        archive.writestr("haybale_fake-1.0.dist-info/METADATA", "Name: haybale-fake\n")
    with pytest.raises(MarketplaceError, match="no haybale.toml"):
        read_wheel_metadata(wheel, "haybale-fake")


def test_haybale_toml_without_a_version_is_rejected(tmp_path):
    import zipfile

    wheel = tmp_path / "haybale_fake-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("haybale_fake/haybale.toml", 'name = "haybale-fake"\n')
        archive.writestr("haybale_fake-1.0.dist-info/METADATA", "Name: haybale-fake\n")
    # The framework's own strict reader is what rejects it, so this gate and a
    # studio's load-time check cannot disagree.
    with pytest.raises(MarketplaceError, match="version"):
        read_wheel_metadata(wheel, "haybale-fake")


# ─────────────────────────────────────────────────────────────────────────────
# install_spec and version
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("channel", ["stable", "latest"])
def test_pinned_channels_pin_exactly(metadata, channel):
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel=channel)
    assert row.install_spec == f"{DIST}=={VERSION}"
    assert row.version == VERSION


def test_edge_floats_but_still_advertises_a_version(metadata):
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="edge")
    assert row.install_spec == DIST
    # The studio computes "update available" as installed < row.version, so a
    # missing version would mean edge subscribers are never offered an update.
    assert row.version == VERSION


def test_a_row_cannot_advertise_a_version_the_artifact_does_not_carry(metadata):
    row = build_row(metadata, dist_name=DIST, version="9.9.9", channel="stable")
    with pytest.raises(MarketplaceError, match="same object"):
        verify_row_version(row, metadata)


# ─────────────────────────────────────────────────────────────────────────────
# The emitted feed
# ─────────────────────────────────────────────────────────────────────────────


def test_feed_parses_back_through_the_consumers_own_parser(metadata):
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="stable")
    body = emit_feed([row], header="# test")
    parsed = parse_marketstall_body(body)
    assert len(parsed) == 1
    assert parsed[0].name == DIST
    assert parsed[0].version == VERSION
    assert parsed[0].install_spec == f"{DIST}=={VERSION}"
    assert parsed[0].label == "Visiongraph"
    assert parsed[0].authors == [("cansik", "https://github.com/cansik"), ("maybites", "https://github.com/maybites")]


def test_name_and_version_are_exactly_str_not_a_subclass(metadata):
    """The tomlkit trap.

    `isinstance` passes for tomlkit's String — which `toml.dumps` writes as a
    *list of characters* — so asserting on `isinstance` would let the bug
    through while looking correct. A published feed already shipped broken this
    way and parsed fine as TOML.
    """
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="stable")
    body = emit_feed([row], header="# test")
    parsed = parse_marketstall_body(body)[0]
    assert type(parsed.name) is str
    assert type(parsed.version) is str
    # And in the raw document: a corrupted version would be a list here.
    raw = tomllib.loads(body)["haybales"][0]
    assert type(raw["version"]) is str
    assert type(raw["name"]) is str


def test_emitter_refuses_a_str_subclass():
    class TomlkitishString(str):
        pass

    with pytest.raises(TypeError, match="subclass"):
        _format_value(TomlkitishString("0.0.38"))


def test_no_consumer_only_fields_are_ever_emitted(metadata):
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="edge")
    # Even if a row somehow carried them, the field order excludes them.
    row.via = "https://example.invalid/marketplace.toml"
    row.last_seen = "2026-08-29"
    row.stale = True
    body = emit_feed([row], header="# test")
    raw = tomllib.loads(body)["haybales"][0]
    for forbidden in ("via", "last_seen", "stale"):
        assert forbidden not in raw
        assert forbidden not in FIELD_ORDER


def test_field_order_matches_the_consumers_serializer():
    # Not restated here — derived from Haybale._TOML_FIELDS, so a generated
    # feed and a studio-written one stay shape-compatible.
    expected = [f for f in Haybale._TOML_FIELDS if f not in ("via", "last_seen", "stale")]
    assert list(FIELD_ORDER) == expected


def test_empty_fields_are_omitted():
    """Omission follows the consumer's own rule — `to_dict` drops falsy values.

    `on_reload` survives because its default is the non-empty string "none",
    which is what a studio-written marketstall also emits. Matching the
    consumer's serializer here rather than inventing a second rule is the point.
    """
    row = Haybale(name="haybale-x", version="1.0", install_spec="haybale-x==1.0")
    raw = tomllib.loads(emit_feed([row], header="# t"))["haybales"][0]
    assert "notes" not in raw  # empty string
    assert "tags" not in raw  # empty list
    assert "require" not in raw  # undeclared, distinct from a bare name
    assert raw["on_reload"] == "none"


def test_authors_are_written_after_every_bare_key(metadata):
    """A bare key after a table header is parsed *into* that table."""
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="stable")
    lines = [line for line in emit_row(row) if line.strip()]
    first_table = next(i for i, line in enumerate(lines) if line.startswith("[[haybales.authors]]"))
    after = [line for line in lines[first_table:] if not line.startswith("[[") and "=" in line]
    assert all(line.split("=")[0].strip() in ("name", "url") for line in after)


def test_a_row_carries_no_curator_authored_field(metadata):
    """Every field of a row comes from the artifact — `deprecated` included."""
    row = build_row(metadata, dist_name=DIST, version=VERSION, channel="stable")
    assert row.deprecated is None
    raw = tomllib.loads(emit_feed([row], header="# t"))["haybales"][0]
    assert "deprecated" not in raw


# ─────────────────────────────────────────────────────────────────────────────
# The registry
# ─────────────────────────────────────────────────────────────────────────────


def test_the_shipped_registry_reads(tmp_path):
    entries = read_registry(REPO_ROOT / "registry")
    assert [e.name for e in entries] == sorted(e.name for e in entries)
    assert DIST in {e.name for e in entries}


def test_a_registry_filename_must_match_its_name(tmp_path):
    (tmp_path / "haybale-a.toml").write_text('name = "haybale-b"\n', encoding="utf-8")
    # Otherwise `git rm registry/haybale-b.toml` would silently remove nothing.
    with pytest.raises(MarketplaceError, match="filename"):
        read_registry(tmp_path)


def test_removing_a_registry_entry_removes_the_row(tmp_path, metadata):
    """Deleting a registry file must drop that library from every channel."""
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "haybale-a.toml").write_text('name = "haybale-a"\n', encoding="utf-8")
    (registry / "haybale-b.toml").write_text('name = "haybale-b"\n', encoding="utf-8")
    assert {e.name for e in read_registry(registry)} == {"haybale-a", "haybale-b"}

    (registry / "haybale-b.toml").unlink()
    assert {e.name for e in read_registry(registry)} == {"haybale-a"}
