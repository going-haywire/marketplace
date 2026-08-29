"""Turn an "Add a library" issue into a registry entry.

    uv run python scripts/issue_to_registry.py --body-file issue.md --number 42

Parses the Issue Form's rendered body, validates the distribution name, and
writes `registry/<name>.toml`. Nothing else: the gates are `check_submission.py`
and the load gate, and the reviewable artifact is the pull request this feeds.

The issue body is **untrusted input from a stranger**. The distribution name
reaches a file path and a shell command, so it is validated against PEP 508's
name grammar before anything is done with it — a name is not a path fragment
and must not be usable as one.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketplace_lib import MarketplaceError  # noqa: E402

#: PEP 508's distribution-name grammar. Anchored, so `../../etc/passwd` and
#: `haybale-foo; rm -rf /` are rejected rather than sanitised — a name that
#: needs sanitising is not a name.
_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


@dataclass(frozen=True)
class Submission:
    """The three fields the Issue Form collects."""

    name: str
    repo: str
    contact: str


def parse_issue_body(body: str) -> Submission:
    """Read the Issue Form's rendered markdown.

    GitHub renders an Issue Form as `### <label>` followed by the value, so the
    labels in `.github/ISSUE_TEMPLATE/add-library.yml` are what is matched
    here. Changing a label there means changing it here.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        if line.startswith("### "):
            if current:
                sections[current] = "\n".join(buffer).strip()
            current = line[4:].strip().lower()
            buffer = []
        elif current:
            buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()

    def field(label: str) -> str:
        value = sections.get(label, "").strip()
        # The Issue Form writes this for an omitted optional field.
        return "" if value == "_No response_" else value

    name = field("pypi distribution name")
    if not name:
        raise MarketplaceError("the issue has no `PyPI distribution name` — was the Add a library form used?")
    if not _NAME.match(name):
        raise MarketplaceError(
            f"{name!r} is not a valid PyPI distribution name. Names are letters, digits, "
            f"`.`, `-` and `_`, starting and ending alphanumeric."
        )

    return Submission(name=name, repo=field("source repository"), contact=field("contact"))


def write_entry(submission: Submission, registry_dir: Path, *, issue_number: int | None = None) -> Path:
    """Write `registry/<name>.toml`, refusing to overwrite an existing member."""
    path = registry_dir / f"{submission.name}.toml"
    if path.exists():
        raise MarketplaceError(
            f"`{submission.name}` is already in the catalogue ({path}). To update it, do nothing — "
            f"`edge` picks up new releases nightly and the pinned channels at their own cadence."
        )

    lines = [
        "# Membership only. Everything descriptive about this library — label,",
        "# description, tags, version, authors — is read out of the wheel at",
        "# generation time and is NOT authored here.",
        f'name    = "{submission.name}"',
    ]
    if submission.repo:
        lines.append(f'repo    = "{submission.repo}"')
    if submission.contact:
        lines.append(f'contact = "{submission.contact}"')
    lines.append(f'added   = "{date.today().isoformat()}"')
    if issue_number:
        lines.append(f'issue   = "{issue_number}"')

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="issue_to_registry", description="Write a registry entry from an issue.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--number", type=int, default=None)
    parser.add_argument("--github-output", action="store_true", help="Emit name= to $GITHUB_OUTPUT.")
    args = parser.parse_args(argv)

    try:
        submission = parse_issue_body(args.body_file.read_text(encoding="utf-8"))
        path = write_entry(submission, args.repo_root / "registry", issue_number=args.number)
    except MarketplaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {path}", file=sys.stderr)

    if args.github_output:
        import os

        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write(f"name={submission.name}\n")
                handle.write(f"path={path}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
