"""Verify a committed lock: recompile it, and check for yanks.

    uv run python scripts/verify_lock.py --lock locks/stable.toml
    uv run python scripts/verify_lock.py --lock locks/stable.toml --previous-ref origin/main

Two independent checks, both of which must pass before a lock is deployed.

## 1. Recompile and compare

The lock is recompiled from its own inputs and the result compared to what is
committed. This is what stops a lock being hand-edited past a failing check:
the file has to be something the tooling would actually produce.

It also needs the lock this one replaces. The solve never lets a library fall
below its previous stable pin, and holds a conflicting library back *to* that
pin, so without it a recompile cannot reproduce any lock in which something was
held back. The previous lock is read from git (`--previous-ref`, the PR's base
branch in CI), never from the lock under review: a hand-edited pin must not be
able to justify itself.

The check is only sound because the resolution is time-pinned. `--exclude-newer`
is read *out of the lock* and reused verbatim, so CI and the curator see the
same PyPI. Without that, a release appearing between the curator's solve and
CI's recompile would make the two disagree legitimately, and the build would
fail for no reason at all — the classic way a reproducibility check gets
disabled for being noisy.

## 2. Yank check

Under PEP 592 a yanked release is ignored by resolvers **unless the requirement
pins that exact version with `==`** — which every `stable` and `latest` row
does. An author's "do not use this release" signal is precisely the one our
pinning defeats, so it is checked explicitly rather than being left to the
resolver, which will silently install a yanked version when asked this way.

Neither check imports anything. This script is safe to run in the privileged
tier; the load gate that does import lives in `verify.yml`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketplace_lib import (  # noqa: E402
    FRAMEWORK_PACKAGES,
    Lock,
    MarketplaceError,
    fetch_release,
    read_lock,
    read_registry,
)
from solve_stable import read_constraints, release_banner_problem, run_resolve  # noqa: E402


def check_yanks(lock: Lock) -> list[str]:
    """Every pinned version that PyPI reports as yanked.

    Checks the framework pins too: a yanked haywire-core would be installed by
    the studio's own constraints file exactly as readily as a yanked library.
    """
    problems: list[str] = []
    for name, version in sorted({**lock.framework, **lock.pins}.items()):
        try:
            release = fetch_release(name, version)
        except MarketplaceError as exc:
            problems.append(f"{name}=={version}: {exc}")
            continue
        if release.yanked:
            detail = f" ({release.yanked_reason})" if release.yanked_reason else ""
            problems.append(
                f"{name}=={version} is YANKED{detail}. An `==` pin installs a yanked "
                f"release anyway (PEP 592), so this must be re-solved, not shipped."
            )
    return problems


def resolves_together(lock: Lock, repo_root: Path) -> tuple[bool, str]:
    """Is the committed set still a valid solution at all?

    The pins are fed back as exact requirements. A narrower question than the
    re-solve below, and worth asking separately because its failure has a
    different cause and a different fix: these versions cannot co-install,
    rather than merely not being what the solver would pick.
    """
    entries = read_registry(repo_root / "registry")
    constraints = [c.spec for c in read_constraints(repo_root / "stable.constraints.txt")]

    requirements = [f"{name}=={lock.framework[name]}" for name in FRAMEWORK_PACKAGES if name in lock.framework]
    for entry in entries:
        version = lock.pins.get(entry.name)
        if version:
            requirements.append(f"{entry.name}=={version}")

    outcome = run_resolve(requirements, constraints, exclude_newer=lock.exclude_newer)
    return outcome.ok, outcome.output


def resolve_from_scratch(lock: Lock, repo_root: Path, *, previous: Lock | None = None) -> dict[str, str]:
    """Re-run the solve from the lock's own inputs and return what it produces.

    **This is the check that stops a lock being hand-edited.** Merely asking
    whether the committed pins still co-install is not enough: a curator who
    edits one pin down to an older version usually produces a set that resolves
    perfectly well, and such a lock would sail through. The question that
    catches it is the stronger one — *would the solver have produced this?*

    It is a fair question to ask only because the resolution is time-pinned.
    `exclude_newer` comes out of the lock, so this re-solve sees exactly the
    PyPI the curator's solve saw, and the same inputs give the same answer. Drop
    the timestamp and this check becomes noise: a release published in between
    would make CI and the curator disagree legitimately, and the first thing
    anyone would do is switch the check off.

    *previous* is the lock this one replaces, as the curator's solve saw it:
    the floors, and the versions a conflicting library is held back to. It must
    come from somewhere the change under review cannot edit (the base branch),
    never from the committed lock itself. Passing the lock as its own floor
    source would let an edited pin become its own justification, which is
    exactly the tampering being checked for. `None` means there was no previous
    lock, and every floor is the newest release.
    """
    from solve_stable import solve

    result = solve(
        repo_root=repo_root,
        framework=dict(lock.framework),
        exclude_newer=lock.exclude_newer,
        previous=previous,
    )
    return result.pins


def read_previous_lock(repo_root: Path, ref: str, lock_path: Path) -> Lock | None:
    """The lock at *ref* (e.g. `origin/main`), or None if *ref* has none.

    Read with `git show`, so it is the committed state of the base branch and
    not anything in the working tree the change under review could touch.
    """
    try:
        relative = lock_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise MarketplaceError(f"{lock_path} is not inside {repo_root}") from exc

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False)

    if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise MarketplaceError(
            f"git ref {ref!r} not found, so the previous lock cannot be read. Fetch it "
            f"(`git fetch origin main`) or pass --previous-ref."
        )
    shown = git("show", f"{ref}:{relative}")
    if shown.returncode != 0:
        return None  # the first lock ever: nothing to hold anything back to
    return Lock.from_toml(shown.stdout)


def verify(lock_path: Path, repo_root: Path, *, previous: Lock | None = None) -> list[str]:
    """Every problem found with *lock_path*. Empty means it may be deployed."""
    lock = read_lock(lock_path)
    problems: list[str] = []

    if not lock.exclude_newer:
        problems.append(
            f"{lock_path}: no `exclude_newer` in [meta]. The recompile check is unsound "
            f"without it — CI and the curator would resolve against different PyPIs."
        )
        return problems

    missing = [name for name in FRAMEWORK_PACKAGES if name not in lock.framework]
    if missing:
        problems.append(
            f"{lock_path}: [framework] is missing {', '.join(missing)}. A stable set must be "
            f"solved against all three packages the studio's install path pins."
        )

    # The README names the current release; it must name *this* one.
    readme = repo_root / "README.md"
    if lock.channel == "stable" and lock.curation_tag and readme.is_file():
        problem = release_banner_problem(readme, lock)
        if problem:
            problems.append(problem)

    entries = {entry.name for entry in read_registry(repo_root / "registry")}
    orphans = sorted(set(lock.pins) - entries)
    if orphans:
        problems.append(
            f"{lock_path}: pins {', '.join(orphans)}, which are not in registry/. "
            f"Re-run the solver — a removed library must leave the lock too."
        )

    ok, output = resolves_together(lock, repo_root)
    if not ok:
        problems.append(
            "the committed set does not co-install. These versions cannot be installed "
            "alongside each other at all, which is the one thing `stable` promises. "
            "Re-run scripts/solve_stable.py. Resolver output:\n\n" + output.strip()
        )
        # No point re-solving: the inputs are already known not to work.
        problems.extend(check_yanks(lock))
        return problems

    # The check that catches hand-editing: would the solver have produced this?
    try:
        expected = resolve_from_scratch(lock, repo_root, previous=previous)
    except MarketplaceError as exc:
        problems.append(f"could not reproduce the lock from its own inputs: {exc}")
        problems.extend(check_yanks(lock))
        return problems

    for name in sorted(set(expected) | set(lock.pins)):
        committed = lock.pins.get(name)
        produced = expected.get(name)
        if committed == produced:
            continue
        if committed is None:
            problems.append(
                f"{name}: the solver puts {produced} in the set but the lock does not carry it. "
                f"Re-run scripts/solve_stable.py rather than editing the lock."
            )
        elif produced is None:
            problems.append(
                f"{name}: the lock pins {committed} but the solver leaves it out of the set. "
                f"Re-run scripts/solve_stable.py rather than editing the lock."
            )
        else:
            problems.append(
                f"{name}: the lock pins {committed} but re-solving from the same inputs "
                f"produces {produced}. Do not hand-edit a lock to get past a failing check — "
                f"re-run scripts/solve_stable.py, and use stable.constraints.txt if you "
                f"disagree with what it chose."
            )

    problems.extend(check_yanks(lock))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_lock", description="Recompile a lock and check for yanks.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--lock", type=Path, default=None, help="Default: locks/stable.toml")
    parser.add_argument("--yanks-only", action="store_true", help="Skip the recompile; check yanks only.")
    parser.add_argument(
        "--previous-ref",
        default="origin/main",
        help="Git ref holding the lock this one replaces (the PR's base branch). Default: origin/main.",
    )
    args = parser.parse_args(argv)

    lock_path = args.lock or (args.repo_root / "locks" / "stable.toml")

    try:
        if args.yanks_only:
            problems = check_yanks(read_lock(lock_path))
        else:
            previous = read_previous_lock(args.repo_root, args.previous_ref, lock_path)
            problems = verify(lock_path, args.repo_root, previous=previous)
    except MarketplaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if problems:
        print(f"{lock_path}: {len(problems)} problem(s)\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1

    print(f"{lock_path}: ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
