"""Which catalogue libraries have releases newer than the stable lock.

    uv run python scripts/report_newer.py --out newer.md

Purely informational. It makes no decision, promotes nothing, and changes no
pin — it is the notification that starts a curation cycle, and the curator's
next step is to run `scripts/solve_stable.py` and read the lock diff.

Deliberately not a gate: a library being behind is the normal state between
curation tags, and turning that into a failing check would train everyone to
ignore it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketplace_lib import (  # noqa: E402
    MarketplaceError,
    fetch_all_releases,
    newest_version,
    read_lock,
    read_registry,
)


def build_report(repo_root: Path) -> tuple[str, int]:
    """The markdown body, and how many libraries are behind."""
    lock = read_lock(repo_root / "locks" / "stable.toml")
    entries = read_registry(repo_root / "registry")

    behind: list[tuple[str, str, str]] = []
    unlisted: list[tuple[str, str]] = []
    errors: list[str] = []

    for entry in entries:
        try:
            newest = newest_version(fetch_all_releases(entry.name))
        except MarketplaceError as exc:
            errors.append(f"`{entry.name}`: {exc}")
            continue
        pinned = lock.pins.get(entry.name)
        if pinned is None:
            unlisted.append((entry.name, newest))
        elif pinned != newest:
            behind.append((entry.name, pinned, newest))

    lines = [
        "The scheduled job found releases newer than the current `stable` lock.",
        "",
        f"Lock solved against `haywire-core=={lock.framework.get('haywire-core', '?')}` "
        f"with PyPI frozen at `{lock.exclude_newer}`.",
        "",
        "This issue is informational — it makes no decision and changes no pin.",
        "",
    ]

    if behind:
        lines += [
            "## Newer than the lock",
            "",
            "| library | in `stable` | newest on PyPI |",
            "| --- | --- | --- |",
        ]
        lines += [f"| `{name}` | {pinned} | **{newest}** |" for name, pinned, newest in behind]
        lines.append("")

    if unlisted:
        lines += [
            "## In the registry but not in the lock",
            "",
            "These could not be fitted alongside the set last time, or joined after it was solved.",
            "",
            "| library | newest on PyPI |",
            "| --- | --- |",
        ]
        lines += [f"| `{name}` | {newest} |" for name, newest in unlisted]
        lines.append("")

    if errors:
        lines += ["## Could not be checked", ""] + [f"- {problem}" for problem in errors] + [""]

    lines += [
        "## To start a curation cycle",
        "",
        "```sh",
        "uv run python scripts/solve_stable.py \\",
        f"  --framework-version {lock.framework.get('haywire-core', 'X.Y.Z')} \\",
        f"  --nicegui {lock.framework.get('nicegui', 'X.Y.Z')} \\",
        "  --latest --curation-tag $(date +%Y.%m.%d)",
        "```",
        "",
        "Then read the diff of `locks/stable.toml` and `stable-report.md`, and open a PR.",
        "Only if you disagree with a relaxation does `stable.constraints.txt` come into it.",
        "",
    ]

    return "\n".join(lines), len(behind) + len(unlisted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="report_newer", description="Report releases newer than the stable lock.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        body, count = build_report(args.repo_root)
    except MarketplaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        args.out.write_text(body, encoding="utf-8")
    else:
        print(body)

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"has_newer={'true' if count else 'false'}\n")
            handle.write(f"count={count}\n")

    print(f"{count} library(ies) behind the lock", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
