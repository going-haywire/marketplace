"""The metadata gate: is this distribution listable at all?

    uv run python scripts/check_submission.py --all
    uv run python scripts/check_submission.py --name haybale-foo

Four checks, all of them answerable by unzipping:

1. the distribution resolves on PyPI, and has a wheel;
2. its wheel contains a `haybale.toml` that passes the framework's strict
   reader — the same reader a studio runs at decoration time, so this gate and
   the runtime cannot disagree about what a valid haybale is;
3. it declares a `haywire.libraries` entry point, without which a studio could
   install it but never load it;
4. the name is not already claimed by another registry entry or by the Official
   feed.

**Nothing here imports the package.** That is what lets this run anywhere,
including in the privileged tier. Whether the library actually *loads* is a
separate question, answered by `check_load.py` in the untrusted tier.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketplace_lib import (  # noqa: E402
    USER_AGENT,
    MarketplaceError,
    build_row,
    download_wheel,
    fetch_all_releases,
    newest_version,
    read_registry,
    read_wheel_metadata,
)

#: The framework's own feed. A name it already carries cannot be claimed here:
#: a subscriber to both would be asked to resolve a collision between two
#: different libraries wearing one name, which the marketplace has no namespace
#: to prevent.
OFFICIAL_FEED_URL = "https://going-haywire.github.io/haywire/marketplace.toml"


@dataclass
class Verdict:
    """The result for one candidate, written for the person who submitted it."""

    name: str
    ok: bool
    checks: list[tuple[str, bool, str]]

    def as_markdown(self) -> str:
        lines = [f"### `{self.name}` — {'PASSED' if self.ok else 'FAILED'}", ""]
        for label, passed, detail in self.checks:
            mark = "x" if passed else " "
            lines.append(f"- [{mark}] {label}" + (f" — {detail}" if detail else ""))
        return "\n".join(lines)


def official_feed_names(timeout: float = 30.0) -> set[str]:
    """Library names the Official feed already carries.

    A network failure yields an empty set rather than an error: this check
    prevents a name collision, and being unable to reach the feed is not
    evidence of one. The curator reviewing the PR is the backstop.
    """
    request = urllib.request.Request(OFFICIAL_FEED_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError):
        print(f"warning: could not fetch {OFFICIAL_FEED_URL}; skipping the name-collision check", file=sys.stderr)
        return set()

    data = tomllib.loads(body)
    names = {str(row["name"]) for row in data.get("haybales", []) if isinstance(row, dict) and row.get("name")}
    # The Official feed is two-tier: names also live in the stalls it lists.
    for stall in data.get("stalls", []):
        url = stall.get("url") if isinstance(stall, dict) else None
        if not url:
            continue
        try:
            stall_request = urllib.request.Request(str(url), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(stall_request, timeout=timeout) as response:
                stall_data = tomllib.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, tomllib.TOMLDecodeError):
            continue
        names |= {
            str(row["name"]) for row in stall_data.get("haybales", []) if isinstance(row, dict) and row.get("name")
        }
    return names


def check(name: str, *, registry_names: set[str], official: set[str], work_dir: Path) -> Verdict:
    """Run every gate for one distribution."""
    checks: list[tuple[str, bool, str]] = []

    if name in official:
        checks.append(
            (
                "the name is not already claimed",
                False,
                f"`{name}` is already published by the Official feed. A subscriber to both would "
                f"see two different libraries wearing one name.",
            )
        )
        return Verdict(name=name, ok=False, checks=checks)
    if list(registry_names).count(name) > 1:
        checks.append(("the name is not already claimed", False, "duplicate registry entry"))
        return Verdict(name=name, ok=False, checks=checks)
    checks.append(("the name is not already claimed", True, ""))

    try:
        releases = fetch_all_releases(name)
        version = newest_version(releases)
        release = releases[version]
    except MarketplaceError as exc:
        checks.append(("the distribution resolves on PyPI", False, str(exc)))
        return Verdict(name=name, ok=False, checks=checks)
    checks.append(("the distribution resolves on PyPI", True, f"newest is {version}"))

    if not release.wheel_url:
        checks.append(
            (
                "it ships a wheel",
                False,
                "PyPI has no wheel for this version. Row metadata is read out of a wheel — "
                "building an sdist to read it would mean executing its setup, which this "
                "gate must never do.",
            )
        )
        return Verdict(name=name, ok=False, checks=checks)
    checks.append(("it ships a wheel", True, ""))

    try:
        wheel = download_wheel(release.wheel_url, work_dir / f"{name}-{version}.whl")
        metadata = read_wheel_metadata(wheel, name)
    except MarketplaceError as exc:
        checks.append(("its wheel contains a valid haybale.toml", False, str(exc)))
        return Verdict(name=name, ok=False, checks=checks)
    checks.append(("its wheel contains a valid haybale.toml", True, ""))

    if not metadata.declares_library_entry_point:
        checks.append(
            (
                "it declares a `haywire.libraries` entry point",
                False,
                "without one a studio can install this but never load it. Add it to your "
                "pyproject.toml:\n\n"
                "      [project.entry-points.'haywire.libraries']\n"
                "      yourlib = 'your_module:Library'",
            )
        )
        return Verdict(name=name, ok=False, checks=checks)
    checks.append(("it declares a `haywire.libraries` entry point", True, ""))

    try:
        row = build_row(metadata, dist_name=name, version=version, channel="latest")
    except MarketplaceError as exc:
        checks.append(("a row can be built from it", False, str(exc)))
        return Verdict(name=name, ok=False, checks=checks)
    checks.append(("a row can be built from it", True, f"`{row.label or row.name}` — {row.description[:80]}"))

    return Verdict(name=name, ok=True, checks=checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_submission", description="The metadata gate.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--name", action="append", help="Check this distribution (repeatable).")
    parser.add_argument("--all", action="store_true", help="Check every entry in registry/.")
    parser.add_argument("--markdown", type=Path, default=None, help="Write the verdict here, for issue-ops.")
    args = parser.parse_args(argv)

    entries = read_registry(args.repo_root / "registry")
    registry_names = [e.name for e in entries]

    if args.all:
        names = registry_names
    elif args.name:
        names = list(args.name)
    else:
        parser.error("pass --all or --name")

    official = official_feed_names()

    verdicts: list[Verdict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in names:
            verdict = check(name, registry_names=set(registry_names), official=official, work_dir=Path(tmp))
            verdicts.append(verdict)
            print(verdict.as_markdown(), file=sys.stderr)
            print(file=sys.stderr)

    if args.markdown:
        body = "\n\n".join(v.as_markdown() for v in verdicts)
        args.markdown.write_text(body + "\n", encoding="utf-8")

    return 0 if all(v.ok for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
