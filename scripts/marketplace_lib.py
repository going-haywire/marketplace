"""Shared plumbing: the registry, PyPI, wheels, and locks.

The three scripts (`generate_feed`, `solve_stable`, `verify_lock`) all need to
read the registry, ask PyPI about a distribution, and pull metadata out of a
wheel. They share those here so there is one definition of each rather than
three that drift.

**Nothing in this module imports a downloaded package.** Reading a library's
metadata is unzipping a wheel and parsing text — an operation that never
executes the payload. That is what lets the privileged (write-capable)
workflows generate rows at all; see README.md, "The permission split". If you
are tempted to add an import here, it belongs in the untrusted load gate
instead.
"""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from haywire.core.library.haybale import Haybale
from haywire.core.library.haybale_toml import HAYBALE_TOML, HaybaleTomlError, module_of
from haywire.core.marketstall.requirement import haywire_core_requirement

USER_AGENT = "haywire-marketplace-generator/1.0 (+https://github.com/going-haywire/marketplace)"

#: The entry point group a haybale must declare to be loadable by a studio.
ENTRY_POINT_GROUP = "haywire.libraries"

#: The three packages a studio's own install path pins. A `stable` set must
#: resolve against all three, not just haywire-core: the studio installs each
#: library with these as constraints, and `nicegui` is a real source of
#: conflicts because libraries depend on it directly.
FRAMEWORK_PACKAGES = ("haywire-core", "haywire-studio", "nicegui")


class MarketplaceError(Exception):
    """A gate failed, or an input is unusable. Carries a message for a human."""


# ─────────────────────────────────────────────────────────────────────────────
# The registry
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegistryEntry:
    """One `registry/<dist-name>.toml` — membership, and nothing else.

    Deliberately tiny. `repo` and `contact` are provenance for curators (who to
    ask about a removal); neither reaches a published row, because every field
    of a row comes from the artifact.
    """

    name: str
    repo: str
    contact: str
    added: str
    path: Path


def read_registry(registry_dir: Path) -> list[RegistryEntry]:
    """Every membership entry, sorted by distribution name.

    Sorted so a regenerated feed diffs cleanly against the previous one:
    filesystem order is not stable across machines, and an unstable row order
    would make every regeneration look like a change.
    """
    entries: list[RegistryEntry] = []
    for path in sorted(registry_dir.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise MarketplaceError(f"{path}: `name` is required and must be the PyPI distribution name")
        if name != path.stem:
            raise MarketplaceError(
                f"{path}: `name` is {name!r} but the filename says {path.stem!r}. "
                f"One file per library, named for the distribution — the mismatch would "
                f"make `git rm registry/{name}.toml` silently remove nothing."
            )
        entries.append(
            RegistryEntry(
                name=name,
                repo=str(data.get("repo", "")),
                contact=str(data.get("contact", "")),
                added=str(data.get("added", "")),
                path=path,
            )
        )
    return sorted(entries, key=lambda e: e.name)


# ─────────────────────────────────────────────────────────────────────────────
# PyPI
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PyPIRelease:
    """One version of one distribution, as PyPI describes it."""

    name: str
    version: str
    wheel_url: str
    yanked: bool
    yanked_reason: str
    uploaded: datetime | None


def _fetch_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MarketplaceError(f"not found on PyPI: {url}") from exc
        raise MarketplaceError(f"PyPI returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise MarketplaceError(f"could not reach PyPI for {url}: {exc.reason}") from exc


def _parse_upload_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _release_from_files(name: str, version: str, files: list[dict[str, Any]]) -> PyPIRelease:
    """Fold PyPI's per-file list for one version into a single release record.

    A version is yanked as a whole — PyPI sets the flag on every file — so
    reading it off any file is the same answer. The wheel URL is what we want
    though: metadata comes out of the wheel, and a distribution that ships only
    an sdist cannot be listed, because reading its metadata would mean building
    it, which means executing its setup.
    """
    wheel_url = ""
    yanked = False
    yanked_reason = ""
    uploaded: datetime | None = None
    for entry in files:
        if entry.get("yanked"):
            yanked = True
            yanked_reason = str(entry.get("yanked_reason") or "")
        stamp = _parse_upload_time(entry.get("upload_time_iso_8601"))
        if stamp is not None and (uploaded is None or stamp < uploaded):
            uploaded = stamp
        if entry.get("packagetype") == "bdist_wheel" and not wheel_url:
            wheel_url = str(entry.get("url", ""))
    return PyPIRelease(
        name=name,
        version=version,
        wheel_url=wheel_url,
        yanked=yanked,
        yanked_reason=yanked_reason,
        uploaded=uploaded,
    )


def fetch_release(name: str, version: str) -> PyPIRelease:
    """One exact version, via PyPI's per-version JSON endpoint."""
    data = _fetch_json(f"https://pypi.org/pypi/{name}/{version}/json")
    return _release_from_files(name, version, list(data.get("urls", [])))


def fetch_all_releases(name: str) -> dict[str, PyPIRelease]:
    """Every version of a distribution, keyed by version string.

    Yanked versions are included: the solver needs to *see* them in order to
    exclude them, and `verify_lock` needs to recognise a pin that has since
    been yanked. Filtering here would make both blind to the thing they check.
    """
    data = _fetch_json(f"https://pypi.org/pypi/{name}/json")
    releases: dict[str, PyPIRelease] = {}
    for version, files in data.get("releases", {}).items():
        if not files:
            # A version with no files left (all deleted) is not installable.
            continue
        releases[version] = _release_from_files(name, version, list(files))
    if not releases:
        raise MarketplaceError(f"{name}: PyPI lists no downloadable releases")
    return releases


def newest_version(releases: dict[str, PyPIRelease], *, allow_prerelease: bool = False) -> str:
    """The newest usable version among *releases*.

    Yanked releases are never "newest": aspiring to one would either fail the
    yank check at deploy time or, worse, pass it by being pinned exactly — the
    one case PEP 592 does not protect. Prereleases are excluded by default for
    the same reason a resolver excludes them: nobody subscribing to a curated
    catalogue asked to be an author's test rig.
    """
    from packaging.version import InvalidVersion, Version

    best: tuple[Version, str] | None = None
    for version, release in releases.items():
        if release.yanked:
            continue
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        if parsed.is_prerelease and not allow_prerelease:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, version)
    if best is None:
        raise MarketplaceError(f"no usable (non-yanked, non-prerelease) release found among {len(releases)} versions")
    return best[1]


# ─────────────────────────────────────────────────────────────────────────────
# Wheels
# ─────────────────────────────────────────────────────────────────────────────


def download_wheel(url: str, dest: Path, *, timeout: float = 120.0) -> Path:
    """Fetch a wheel to *dest*. Downloading is not executing."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            dest.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise MarketplaceError(f"could not download {url}: {exc}") from exc
    return dest


@dataclass(frozen=True)
class WheelMetadata:
    """Everything a row needs, read out of one wheel.

    `haybale` is the parsed `haybale.toml` — the library's own canon for
    everything descriptive. `requires_dist` and `entry_points` come from the
    `.dist-info`, and exist to answer two questions the haybale.toml cannot:
    what framework floor the author declared, and whether a studio can load
    this at all.
    """

    haybale: dict[str, Any]
    requires_dist: list[str]
    entry_points: dict[str, dict[str, str]]
    project_urls: dict[str, str]

    @property
    def declares_library_entry_point(self) -> bool:
        return bool(self.entry_points.get(ENTRY_POINT_GROUP))


def _parse_entry_points(text: str) -> dict[str, dict[str, str]]:
    """Parse an `entry_points.txt` INI body into {group: {name: target}}.

    Hand-parsed rather than via `configparser` because entry point targets
    contain `:` (`haybale_visiongraph:Library`), and configparser's default
    delimiters split on it.
    """
    groups: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = groups.setdefault(line[1:-1], {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return groups


def _parse_project_urls(metadata_lines: list[str]) -> dict[str, str]:
    """`Project-URL: Label, https://…` headers as {label: url}."""
    urls: dict[str, str] = {}
    for value in metadata_lines:
        label, _, url = value.partition(",")
        if url:
            urls[label.strip()] = url.strip()
    return urls


def read_wheel_metadata(wheel_path: Path, dist_name: str) -> WheelMetadata:
    """Unzip *wheel_path* and read the metadata a row is built from.

    Strict on purpose, and the strictness is the metadata gate: a wheel with no
    `haybale.toml`, or one that fails the framework's own reader, is not a
    haybale and cannot be listed. Failing here is how a bad submission is
    caught while it is still a pull request.

    Uses the framework's `read_haybale_toml` — the same reader a studio runs at
    decoration time — so this gate and the runtime cannot disagree about what a
    valid haybale is.
    """
    module = module_of(dist_name)
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()

        haybale_path = f"{module}/{HAYBALE_TOML}"
        if haybale_path not in names:
            # Fall back to a search: a distribution may ship its package under
            # a directory that `module_of` cannot derive from the dist name.
            candidates = [n for n in names if n.endswith(f"/{HAYBALE_TOML}") and n.count("/") == 1]
            if len(candidates) != 1:
                raise MarketplaceError(
                    f"{dist_name}: no {HAYBALE_TOML} in the wheel (looked for {haybale_path!r}). "
                    f"Every haybale ships one beside __init__.py; without it there is no "
                    f"library metadata to publish."
                )
            haybale_path = candidates[0]

        raw_haybale = archive.read(haybale_path).decode("utf-8")

        dist_info = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if dist_info is None:
            raise MarketplaceError(f"{dist_name}: wheel has no .dist-info/METADATA")
        metadata_message = BytesParser().parsebytes(archive.read(dist_info))

        entry_points_name = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
        entry_points = _parse_entry_points(archive.read(entry_points_name).decode("utf-8")) if entry_points_name else {}

    # Written to a temp package dir so the framework's own strict reader — the
    # one a studio runs — is what validates it, rather than a second parser
    # here that could accept what the runtime rejects.
    haybale = _read_haybale_strict(raw_haybale, dist_name)

    return WheelMetadata(
        haybale=haybale,
        requires_dist=[str(v) for v in metadata_message.get_all("Requires-Dist") or []],
        entry_points=entry_points,
        project_urls=_parse_project_urls([str(v) for v in metadata_message.get_all("Project-URL") or []]),
    )


def _read_haybale_strict(raw_text: str, dist_name: str) -> dict[str, Any]:
    """Validate a `haybale.toml` body through the framework's strict reader.

    The reader takes a directory, so the body is written to a scratch one. The
    round-trip is worth it: a private re-implementation of "what is a valid
    haybale.toml" is exactly the kind of duplicate that drifts, and a
    submission accepted here but rejected at load time is the failure mode this
    gate exists to prevent.

    Returns the *whole* parsed document, not just the strict reader's fields:
    the reader deliberately returns only what the runtime loads, while a row
    also carries `origin`, the URL fields and `[[authors]]`.
    """
    import tempfile

    from haywire.core.library.haybale_toml import read_haybale_toml

    with tempfile.TemporaryDirectory() as tmp:
        package_dir = Path(tmp)
        (package_dir / HAYBALE_TOML).write_text(raw_text, encoding="utf-8")
        try:
            read_haybale_toml(package_dir)
        except HaybaleTomlError as exc:
            raise MarketplaceError(f"{dist_name}: {exc}") from exc

    # Parsed a second time with tomllib rather than reusing the tomlkit-backed
    # read: tomlkit's String subclasses `str`, and `toml.dumps` serializes such
    # a value as a *list of characters*. A published feed already shipped
    # broken that way. tomllib yields plain builtins, so nothing tomlkit-shaped
    # can reach the writer. See haywire.core.tomlio.plain.
    return tomllib.loads(raw_text)


# ─────────────────────────────────────────────────────────────────────────────
# Building a row
# ─────────────────────────────────────────────────────────────────────────────

#: Channel names, in the order they are presented to a subscriber — weakest
#: assertion first.
CHANNELS = ("edge", "latest", "stable")


def build_row(metadata: WheelMetadata, *, dist_name: str, version: str, channel: str) -> Haybale:
    """One `[[haybales]]` row, every field of it read from the wheel.

    The curator authors nothing here. That is not a style preference: if a row
    merely *linked* to metadata the author controls, an author could keep
    `name = "haybale-foo"` and repoint `install_spec` at a different
    distribution, and every subscriber would follow on their next refresh.
    Reading the metadata out of the wheel being pinned makes the row and the
    payload the same object.

    `version` is passed in rather than taken from the haybale.toml because the
    channel decides which artifact is being described, and the caller has
    already downloaded that exact wheel. They agree — the wheel's own
    haybale.toml carries the same version — and `verify_row_version` asserts it.

    `via`, `last_seen` and `stale` are never set: those are written by a
    *consumer's* refresh into its own cache, and a publisher emitting them
    would be publishing another machine's state.
    """
    if channel not in CHANNELS:
        raise MarketplaceError(f"unknown channel {channel!r}; expected one of {CHANNELS}")

    haybale = metadata.haybale
    # `edge` floats: its spec resolves at install time. `version` is still
    # refreshed nightly, because the studio computes "update available" as
    # `installed_version < row.version` — a frozen version would mean edge
    # subscribers are never offered an update at all.
    install_spec = dist_name if channel == "edge" else f"{dist_name}=={version}"

    authors: list[tuple[str, str]] = []
    for entry in haybale.get("authors", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        url = entry.get("url")
        authors.append((str(name), str(url) if isinstance(url, str) else ""))

    def text(key: str) -> str:
        value = haybale.get(key)
        # str() rather than a passthrough: belt and braces against the tomlkit
        # trap, so this holds even if the parse above is ever changed back.
        return str(value) if isinstance(value, str) else ""

    def strings(key: str) -> list[str]:
        value = haybale.get(key)
        return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []

    return Haybale(
        name=str(dist_name),
        version=str(version),
        require=haywire_core_requirement(metadata.requires_dist) or "",
        label=text("label"),
        description=text("description"),
        authors=authors,
        source="pypi",
        install_spec=install_spec,
        tags=strings("tags"),
        os=strings("os"),
        on_reload=text("on_reload") or "none",
        linked_libraries=strings("linked_libraries"),
        origin=text("origin"),
        origin_provider=text("origin_provider"),
        notes=text("notes"),
        homepage_url=text("homepage_url"),
        documentation_url=text("documentation_url"),
        issues_url=text("issues_url"),
        examples_path=text("examples_path"),
        tests_path=text("tests_path"),
        # `deprecated` is deliberately not carried: it is the one descriptive
        # field a curator might be tempted to author, and the rule is that a
        # library to be dropped is removed from the registry, never annotated.
        # An author's own notice reaches users through their own stall.
    )


def verify_row_version(row: Haybale, metadata: WheelMetadata) -> None:
    """The row's version must be the one inside the wheel it describes.

    A row that advertised a version the artifact does not carry would break the
    studio's update comparison in the least debuggable way: it would offer an
    update that installs something reporting a different version, forever.
    """
    declared = metadata.haybale.get("version")
    if isinstance(declared, str) and declared and declared != row.version:
        raise MarketplaceError(
            f"{row.name}: the wheel's haybale.toml declares version {declared!r} but the row "
            f"pins {row.version!r}. The row and the artifact must be the same object."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Locks
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Lock:
    """A resolved set: which version of each library, solved against what.

    The `--exclude-newer` timestamp is part of the lock rather than being
    recomputed by each reader. CI recompiles from scratch and fails when the
    result differs from the committed lock — a check that is only sound if both
    sides see the same PyPI, so CI reads this timestamp back and reuses it
    verbatim.
    """

    channel: str
    exclude_newer: str
    framework: dict[str, str] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)
    generated: str = ""
    curation_tag: str = ""

    def to_toml(self) -> str:
        lines = [
            f"# {self.channel} lock — generated by scripts/solve_stable.py. Do not hand-edit:",
            "# lock-check.yml recompiles from these inputs and fails on any difference.",
            "",
            "[meta]",
            f'channel = "{self.channel}"',
            # Read back verbatim by verify_lock so the recompile sees exactly
            # the PyPI the solve saw.
            f'exclude_newer = "{self.exclude_newer}"',
            f'generated = "{self.generated}"',
        ]
        if self.curation_tag:
            lines.append(f'curation_tag = "{self.curation_tag}"')
        lines += ["", "# The framework versions this set was solved against.", "[framework]"]
        for name in FRAMEWORK_PACKAGES:
            if name in self.framework:
                lines.append(f'"{name}" = "{self.framework[name]}"')
        lines += ["", "# The solved set.", "[pins]"]
        for name in sorted(self.pins):
            lines.append(f'"{name}" = "{self.pins[name]}"')
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_toml(cls, text: str) -> Lock:
        data = tomllib.loads(text)
        meta = data.get("meta", {})
        return cls(
            channel=str(meta.get("channel", "")),
            exclude_newer=str(meta.get("exclude_newer", "")),
            generated=str(meta.get("generated", "")),
            curation_tag=str(meta.get("curation_tag", "")),
            framework={str(k): str(v) for k, v in data.get("framework", {}).items()},
            pins={str(k): str(v) for k, v in data.get("pins", {}).items()},
        )


def read_lock(path: Path) -> Lock:
    if not path.is_file():
        raise MarketplaceError(f"{path} not found — run scripts/solve_stable.py first")
    return Lock.from_toml(path.read_text(encoding="utf-8"))


def utc_now_stamp() -> str:
    """An ISO-8601 UTC timestamp, the form `uv pip compile --exclude-newer` takes."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
