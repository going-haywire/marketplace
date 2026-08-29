"""Solve the `stable` set, and explain the result.

    uv run python scripts/solve_stable.py
    uv run python scripts/solve_stable.py --framework-version 0.1.2 --nicegui 3.16.0

Runs to completion unattended. It asks nothing, picks no version by hand, and
produces the same answer every time for the same inputs. A curator reads the
lock diff and `stable-report.md` and either accepts it or writes a line in
`stable.constraints.txt`; there is no intermediate state where a human has to
choose a version.

## Why there is an algorithm here at all

A dependency resolver optimises nothing. It returns *a* solution, preferring
high versions in its own traversal order, so left alone it will silently hold
one library three releases back in order to let an obscure one be current.

This supplies the objective it lacks, by expressing aspiration as floors:

    hard floor,  every library:  >= its current stable pin   # never regresses
    aspiration,  every library:  >= its newest release

    loop:
        resolve(hard floors + aspirations)
        success -> done
        failure -> the derivation names the conflicting libraries.
                   relax ONE named aspiration to its hard floor, record why, loop.

One resolver call per iteration, and **only the libraries the resolver actually
named are candidates** — falling back to "try every library" turns a two-call
loop into an N-call one.

Which named library is relaxed:

1. Whichever relaxation leaves the most libraries at their newest.
2. On a tie, the more recently released one. The rest of the set was working;
   the new release broke the fit, and its author can widen a constraint.
   Self-correcting — next cycle it is no longer the newest.
3. `stable.constraints.txt` overrides both.

## Why not test each library separately

Testing one library at a time against a frozen baseline under-reports. If
`haybale-a 2.0` and `haybale-b 2.0` both need `numpy>=2` while the current pins
hold both below it, A alone fails and B alone fails — and both get reported as
conflicted, when moving *both together* resolves cleanly. Libraries that must
advance together are invisible to a per-library sweep. The loop above handles
them for free, because it never removes an aspiration that was not named.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datetime import UTC

from marketplace_lib import (  # noqa: E402
    FRAMEWORK_PACKAGES,
    Lock,
    MarketplaceError,
    PyPIRelease,
    fetch_all_releases,
    newest_version,
    read_lock,
    read_registry,
    utc_now_stamp,
)


@dataclass
class LibraryState:
    """What the solve knows about one library, and what it decided.

    `floor` never moves — the set may not regress. `aspiration` starts at the
    newest release and is what gets relaxed.
    """

    name: str
    floor: str
    aspiration: str
    releases: dict[str, PyPIRelease]
    relaxed: bool = False
    reason: str = ""
    derivation: str = ""
    held_by: str = ""

    @property
    def requirement(self) -> str:
        """The floor this library contributes to the resolve."""
        return f"{self.name}>={self.aspiration if not self.relaxed else self.floor}"

    @property
    def at_newest(self) -> bool:
        return not self.relaxed


@dataclass
class Constraint:
    """One line of `stable.constraints.txt`, with the reason beside it."""

    spec: str
    reason: str
    name: str


@dataclass
class SolveResult:
    """The solved set, and the record of how it got there."""

    pins: dict[str, str] = field(default_factory=dict)
    states: dict[str, LibraryState] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    iterations: int = 0
    framework: dict[str, str] = field(default_factory=dict)
    exclude_newer: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Constraints
# ─────────────────────────────────────────────────────────────────────────────


def read_constraints(path: Path) -> list[Constraint]:
    """Parse `stable.constraints.txt` — the curator's only knob on the solve.

    pip-tools-style ceilings (`haybale-x<2.0`, a deliberate hold) and exclusions
    (`haybale-x != 1.4.0`, a version that failed the load gate). Every line
    carries a `#` reason, which the report quotes verbatim to the author whose
    release did not land — so a hold is never unexplained.
    """
    if not path.is_file():
        return []
    constraints: list[Constraint] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        spec, _, comment = line.partition("#")
        spec = spec.strip()
        if not spec:
            continue
        name = re.split(r"[<>=!~ \[]", spec, maxsplit=1)[0].strip()
        constraints.append(Constraint(spec=spec, reason=comment.strip(), name=name))
    return constraints


# ─────────────────────────────────────────────────────────────────────────────
# The resolver
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolveOutcome:
    """One `uv pip compile` call: did it solve, and what did it say."""

    ok: bool
    output: str
    pins: dict[str, str] = field(default_factory=dict)


def _parse_compiled_pins(text: str) -> dict[str, str]:
    """Read `name==version` out of a compiled requirements body."""
    pins: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, sep, version = line.partition("==")
        if sep:
            pins[name.strip().lower()] = version.strip()
    return pins


def run_resolve(
    requirements: list[str],
    constraints: list[str],
    *,
    exclude_newer: str,
    timeout: float = 900.0,
) -> ResolveOutcome:
    """One resolver call.

    `--universal` because libraries declare an `os` field, and a set that
    resolves on Linux may not on Windows; universal resolution proves every
    platform in one pass via environment markers, instead of one call per
    platform that can each succeed while no single set satisfies all of them.

    `--exclude-newer` freezes the view of PyPI. Without it, a release appearing
    between the curator's solve and CI's recompile makes the two disagree
    legitimately, and `lock-check` fails for no reason. The timestamp is written
    into the lock and read back by CI.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        req_file = tmp_dir / "requirements.in"
        req_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")

        command = [
            "uv",
            "pip",
            "compile",
            str(req_file),
            "--universal",
            "--exclude-newer",
            exclude_newer,
            "--no-header",
            "--quiet",
        ]
        if constraints:
            constraint_file = tmp_dir / "constraints.txt"
            constraint_file.write_text("\n".join(constraints) + "\n", encoding="utf-8")
            command += ["--constraint", str(constraint_file)]

        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise MarketplaceError("`uv` is not on PATH — the solve shells out to `uv pip compile`") from exc
        except subprocess.TimeoutExpired as exc:
            raise MarketplaceError(f"`uv pip compile` timed out after {timeout}s") from exc

    if completed.returncode == 0:
        return ResolveOutcome(ok=True, output=completed.stdout, pins=_parse_compiled_pins(completed.stdout))
    return ResolveOutcome(ok=False, output=completed.stderr or completed.stdout)


def named_in_derivation(output: str, candidates: list[str]) -> list[str]:
    """Which of *candidates* the resolver's derivation actually named.

    This is the whole reason the loop is short. uv's failure message is a
    derivation chain naming the packages whose requirements clash; only those
    are relaxation candidates. Falling back to every library would turn a
    two-call loop into an N-call one, and would also relax libraries that had
    nothing to do with the conflict.

    Matched on a word boundary against both the hyphen and underscore spellings,
    because uv prints the normalised name and a registry entry may use either.
    """
    named: list[str] = []
    for name in candidates:
        pattern = re.escape(name).replace(r"\-", "[-_]")
        if re.search(rf"(?<![\w-]){pattern}(?![\w-])", output, re.IGNORECASE):
            named.append(name)
    return named


# ─────────────────────────────────────────────────────────────────────────────
# The loop
# ─────────────────────────────────────────────────────────────────────────────


def _release_date(state: LibraryState) -> object:
    """When *state*'s aspiration was published — the tiebreak key."""
    release = state.releases.get(state.aspiration)
    return release.uploaded if release and release.uploaded else None


def _choose_relaxation(
    named: list[str],
    states: dict[str, LibraryState],
    requirements_for,
    constraints: list[str],
    *,
    exclude_newer: str,
) -> tuple[str, ResolveOutcome | None]:
    """Which named library to relax, per the rule in the module docstring.

    Rule 1 is stated directly rather than through a proxy: try relaxing each
    named candidate, keep whichever leaves the most libraries at their newest.
    That costs one resolver call per candidate in the iteration, but the
    candidate list is only the libraries the derivation named — normally two.

    The score is read out of each trial's *resolved pins*, not out of the
    `relaxed` flags. Relaxing a library only lowers its floor; the resolver
    still decides where everything actually lands, and a relaxation can drag
    other libraries down with it. Counting flags would score two candidates
    identically whenever both trials succeed, and hand the decision to the
    tiebreak — which is the objective not being applied at all.

    A candidate whose relaxation *also* fails tells us nothing about the
    objective, so it is kept only as a fallback for the tiebreak.
    """
    best_name = ""
    best_outcome: ResolveOutcome | None = None
    best_score = -1

    for candidate in named:
        state = states[candidate]
        if state.relaxed:
            continue
        state.relaxed = True
        try:
            outcome = run_resolve(requirements_for(), constraints, exclude_newer=exclude_newer)
        finally:
            state.relaxed = False
        if not outcome.ok:
            continue
        # Rule 1: the objective — how many libraries this trial actually leaves
        # at their newest release.
        score = sum(1 for name, s in states.items() if outcome.pins.get(name.lower()) == s.aspiration)
        if score > best_score:
            best_name, best_outcome, best_score = candidate, outcome, score

    if best_name:
        return best_name, best_outcome

    # Nothing tried resolved on its own: this iteration cannot finish the solve,
    # so relax by rule 2 alone and let the next iteration relax the next one.
    # Rule 2 — the more recently released loses. The rest of the set was
    # working; the new release is what broke the fit, and its author is the
    # person who can widen a constraint.
    unrelaxed = [states[n] for n in named if not states[n].relaxed]
    if not unrelaxed:
        return "", None
    from datetime import datetime

    epoch = datetime.min.replace(tzinfo=UTC)
    newest_released = max(unrelaxed, key=lambda s: _release_date(s) or epoch)
    return newest_released.name, None


def solve(
    *,
    repo_root: Path,
    framework: dict[str, str],
    exclude_newer: str,
    previous: Lock | None,
    max_iterations: int = 24,
) -> SolveResult:
    """Run the relaxation loop to completion."""
    entries = read_registry(repo_root / "registry")
    if not entries:
        raise MarketplaceError("registry/ holds no entries — nothing to solve")

    constraints = read_constraints(repo_root / "stable.constraints.txt")
    constraint_specs = [c.spec for c in constraints]
    held = {c.name: c for c in constraints}

    states: dict[str, LibraryState] = {}
    for entry in entries:
        releases = fetch_all_releases(entry.name)
        newest = newest_version(releases)
        # The hard floor is the current stable pin, so the set can never go
        # backwards. A library new to the registry has no previous pin; its
        # floor is its newest, which is also its aspiration — it either fits at
        # that version or is left out of the set entirely.
        floor = (previous.pins.get(entry.name) if previous else None) or newest
        states[entry.name] = LibraryState(
            name=entry.name,
            floor=floor,
            aspiration=newest,
            releases=releases,
            held_by=held[entry.name].spec if entry.name in held else "",
        )

    framework_requirements = [f"{name}=={framework[name]}" for name in FRAMEWORK_PACKAGES if name in framework]

    def requirements_for() -> list[str]:
        return framework_requirements + [s.requirement for s in states.values()]

    result = SolveResult(states=states, constraints=constraints, framework=framework, exclude_newer=exclude_newer)

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration
        outcome = run_resolve(requirements_for(), constraint_specs, exclude_newer=exclude_newer)
        if outcome.ok:
            for name in states:
                pinned = outcome.pins.get(name.lower())
                if pinned:
                    result.pins[name] = pinned
            return result

        candidates = [n for n, s in states.items() if not s.relaxed]
        named = named_in_derivation(outcome.output, candidates)
        if not named:
            raise MarketplaceError(
                "the resolver failed but named no curated library, so there is nothing to "
                "relax — the conflict is between the framework pins and a transitive "
                "dependency, not between catalogue members. Resolver output:\n\n" + outcome.output.strip()
            )

        chosen, _ = _choose_relaxation(named, states, requirements_for, constraint_specs, exclude_newer=exclude_newer)
        if not chosen:
            raise MarketplaceError(
                "every named library is already relaxed to its floor and the set still does "
                "not resolve. The current stable pins cannot be reproduced against these "
                "framework versions; a constraint in stable.constraints.txt is needed. "
                "Resolver output:\n\n" + outcome.output.strip()
            )

        state = states[chosen]
        state.relaxed = True
        others = [n for n in named if n != chosen]
        state.reason = (
            f"taking {state.aspiration} would have forced "
            + (", ".join(others) if others else "the rest of the set")
            + " back"
        )
        state.derivation = outcome.output.strip()

    raise MarketplaceError(f"the solve did not settle in {max_iterations} iterations")


# ─────────────────────────────────────────────────────────────────────────────
# The report
# ─────────────────────────────────────────────────────────────────────────────

# A markdown table; its rows are wider than the code line limit by nature.
# ruff: noqa: E501 is file-scoped, so the exemption is narrowed to this literal.
_REPORT_HEADER = """\
# Why each library sits at the version it does

Generated by `scripts/solve_stable.py` alongside `locks/stable.toml`, from the
same run — so it cannot describe a different set than the one that shipped.

If your newest release is not in `stable`, find your library below. The reason
is one of four, and each says what to do about it.

| reason | meaning | what to do |
| --- | --- | --- |
| `conflict` | your version's dependencies cannot be satisfied alongside the rest of the set | widen the constraint if you can, or wait for the other library to move |
| `failed-load` | it installed, then failed to import | fix and release |
| `held` | a curator deliberately pinned the set below your version | read the reason; open an issue if you disagree |
| `not-yet-considered` | you released after this run | nothing — the next run picks it up |

Solved against {framework}, with PyPI frozen at `{exclude_newer}`.
Settled in {iterations} resolver iteration(s).
"""


def render_report(result: SolveResult) -> str:
    """`stable-report.md` — written for library authors, not for curators.

    This is the document an author is pointed at when their newest release did
    not land, so every gap carries the resolver's own explanation and what
    taking the newer version would have cost, rather than a bare "conflict".
    """
    framework = ", ".join(f"`{name}=={version}`" for name, version in sorted(result.framework.items()))
    parts = [
        _REPORT_HEADER.format(
            framework=framework or "the framework pins",
            exclude_newer=result.exclude_newer,
            iterations=result.iterations,
        ),
        "## The set",
        "",
        "| library | newest | in `stable` | state |",
        "| --- | --- | --- | --- |",
    ]

    details: list[tuple[str, LibraryState, str]] = []
    for name in sorted(result.states):
        state = result.states[name]
        pinned = result.pins.get(name, "—")
        if pinned == "—":
            reason = "not-in-set"
        elif pinned == state.aspiration:
            reason = "current"
        elif state.held_by:
            reason = "held"
        elif state.relaxed:
            reason = "conflict"
        else:
            # The resolver chose a version below the aspiration without the
            # loop relaxing anything — a transitive dependency's ceiling, not a
            # decision this solve made.
            reason = "constrained-by-dependency"
        parts.append(f"| `{name}` | {state.aspiration} | {pinned} | `{reason}` |")
        if reason in ("held", "conflict", "not-in-set", "constrained-by-dependency"):
            details.append((reason, state, pinned))

    if details:
        parts += ["", "## Why", ""]
        for reason, state, pinned in details:
            parts.append(f"### `{state.name}` — {reason}")
            parts.append("")
            if reason == "held":
                parts.append(f"Held by a line in `stable.constraints.txt`: `{state.held_by}`")
                held = next((c for c in result.constraints if c.name == state.name), None)
                if held and held.reason:
                    parts += ["", f"> {held.reason}"]
            elif reason == "conflict":
                parts.append(
                    f"`stable` pins {pinned} rather than {state.aspiration}: {state.reason}. "
                    f"The resolver's own derivation:"
                )
                parts += ["", "```text", state.derivation or "(no derivation captured)", "```"]
            elif reason == "not-in-set":
                parts.append(
                    "This library is in the registry but not in the solved set — no version of "
                    "it could be fitted alongside the others against these framework pins."
                )
            else:
                parts.append(
                    f"`stable` pins {pinned} rather than {state.aspiration}, but the solve "
                    f"relaxed nothing for it: a dependency of another library caps it. Nothing "
                    f"to do here — it moves when that cap does."
                )
            parts.append("")

    parts += [
        "",
        "---",
        "",
        "A library missing from this table entirely is not in the catalogue. See the",
        "[README](https://github.com/going-haywire/marketplace#getting-your-library-listed)",
        "for how to get listed.",
        "",
    ]
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solve_stable",
        description="Solve the stable set and write locks/stable.toml + stable-report.md.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--framework-version",
        required=True,
        help="The haywire release to solve against; pins haywire-core and haywire-studio.",
    )
    parser.add_argument("--nicegui", required=True, help="The nicegui version that release pins.")
    parser.add_argument(
        "--exclude-newer",
        default="",
        help="Freeze PyPI at this ISO-8601 UTC timestamp. Default: now.",
    )
    parser.add_argument("--curation-tag", default="", help="Recorded in the lock.")
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Also write locks/latest.toml — every library at its newest, unsolved.",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    exclude_newer = args.exclude_newer or utc_now_stamp()
    framework = {
        "haywire-core": args.framework_version,
        "haywire-studio": args.framework_version,
        "nicegui": args.nicegui,
    }

    lock_path = repo_root / "locks" / "stable.toml"
    previous = read_lock(lock_path) if lock_path.is_file() else None

    try:
        result = solve(
            repo_root=repo_root,
            framework=framework,
            exclude_newer=exclude_newer,
            previous=previous,
        )
    except MarketplaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    lock = Lock(
        channel="stable",
        exclude_newer=exclude_newer,
        framework=framework,
        pins=result.pins,
        generated=utc_now_stamp(),
        curation_tag=args.curation_tag,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(lock.to_toml(), encoding="utf-8")
    print(f"wrote {lock_path} ({len(result.pins)} libraries, {result.iterations} iteration(s))", file=sys.stderr)

    report_path = repo_root / "stable-report.md"
    report_path.write_text(render_report(result), encoding="utf-8")
    print(f"wrote {report_path}", file=sys.stderr)

    if args.latest:
        # `latest` is not solved: it is each library at its newest, and the
        # co-install proof is exactly what it does *not* assert. Its gate is
        # the untrusted load check, one library at a time.
        latest_lock = Lock(
            channel="latest",
            exclude_newer=exclude_newer,
            framework=framework,
            pins={name: state.aspiration for name, state in result.states.items()},
            generated=utc_now_stamp(),
            curation_tag=args.curation_tag,
        )
        latest_path = repo_root / "locks" / "latest.toml"
        latest_path.write_text(latest_lock.to_toml(), encoding="utf-8")
        print(f"wrote {latest_path} ({len(latest_lock.pins)} libraries)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
