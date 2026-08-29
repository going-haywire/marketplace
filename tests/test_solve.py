"""The relaxation loop's decision-making, with the resolver stubbed out.

The resolver itself is uv's problem. What is tested here is the part this repo
owns: which library gets relaxed, that only *named* libraries are candidates,
and that libraries which must advance together are not falsely reported as
conflicted.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import solve_stable  # noqa: E402
from marketplace_lib import Lock, PyPIRelease  # noqa: E402
from solve_stable import (  # noqa: E402
    named_in_derivation,
    read_constraints,
    render_report,
    solve,
)


def release(name: str, version: str, *, day: int = 1, yanked: bool = False) -> PyPIRelease:
    return PyPIRelease(
        name=name,
        version=version,
        wheel_url=f"https://pypi.invalid/{name}-{version}.whl",
        yanked=yanked,
        yanked_reason="",
        uploaded=datetime(2026, 8, day, tzinfo=UTC),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reading the derivation
# ─────────────────────────────────────────────────────────────────────────────


def test_only_libraries_the_resolver_named_are_candidates():
    output = (
        "  × No solution found when resolving dependencies:\n"
        "  ╰─▶ Because haybale-a==2.0 depends on numpy>=2 and haybale_b==2.0 depends on numpy<2,\n"
        "      we can conclude that the requirements are unsatisfiable."
    )
    named = named_in_derivation(output, ["haybale-a", "haybale-b", "haybale-c"])
    # haybale-c had nothing to do with the conflict; relaxing it would be both
    # wasted work and an unexplained hold on an innocent library.
    assert named == ["haybale-a", "haybale-b"]


def test_derivation_matching_accepts_the_normalised_spelling():
    # uv prints the normalised name; a registry entry may use either spelling.
    assert named_in_derivation("haybale_visiongraph==1.0", ["haybale-visiongraph"]) == ["haybale-visiongraph"]


def test_a_substring_is_not_a_match():
    assert named_in_derivation("haybale-visiongraph-extras==1.0", ["haybale-visiongraph"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# Constraints
# ─────────────────────────────────────────────────────────────────────────────


def test_constraints_carry_their_reason(tmp_path):
    path = tmp_path / "stable.constraints.txt"
    path.write_text(
        "# a comment line\n"
        "\n"
        "haybale-x<2.0        # 2.x drops the camera API the examples use\n"
        "haybale-y!=1.4.0     # 1.4.0 fails to import on macOS\n",
        encoding="utf-8",
    )
    constraints = read_constraints(path)
    assert [c.name for c in constraints] == ["haybale-x", "haybale-y"]
    assert constraints[0].spec == "haybale-x<2.0"
    # Quoted verbatim to the author whose release did not land, so a hold is
    # never unexplained.
    assert constraints[0].reason == "2.x drops the camera API the examples use"


def test_a_missing_constraints_file_is_not_an_error(tmp_path):
    assert read_constraints(tmp_path / "nope.txt") == []


# ─────────────────────────────────────────────────────────────────────────────
# The loop
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path):
    """A three-library registry, plus the repo shape the solve reads."""
    (tmp_path / "registry").mkdir()
    for name in ("haybale-a", "haybale-b", "haybale-c"):
        (tmp_path / "registry" / f"{name}.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    return tmp_path


def _stub_pypi(monkeypatch, catalogue: dict[str, dict[str, PyPIRelease]]):
    monkeypatch.setattr(solve_stable, "fetch_all_releases", lambda name: catalogue[name])


def test_a_set_that_fits_is_taken_whole(registry, monkeypatch):
    catalogue = {
        name: {"1.0": release(name, "1.0"), "2.0": release(name, "2.0", day=20)}
        for name in ("haybale-a", "haybale-b", "haybale-c")
    }
    _stub_pypi(monkeypatch, catalogue)

    calls: list[list[str]] = []

    def fake_resolve(requirements, constraints, *, exclude_newer, timeout=900.0):
        calls.append(requirements)
        pins = {name: "2.0" for name in ("haybale-a", "haybale-b", "haybale-c")}
        return solve_stable.ResolveOutcome(ok=True, output="", pins=pins)

    monkeypatch.setattr(solve_stable, "run_resolve", fake_resolve)

    result = solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=None,
    )
    assert result.pins == {"haybale-a": "2.0", "haybale-b": "2.0", "haybale-c": "2.0"}
    assert result.iterations == 1  # one resolver call when everything fits
    # The framework is pinned as three packages, not one: the studio's install
    # path constrains all three and nicegui is a real source of conflicts.
    assert "haywire-core==0.1.2" in calls[0]
    assert "haywire-studio==0.1.2" in calls[0]
    assert "nicegui==3.16.0" in calls[0]


def test_libraries_that_must_advance_together_are_not_reported_as_conflicted(registry, monkeypatch):
    """The failure a per-library sweep cannot see.

    `haybale-a 2.0` and `haybale-b 2.0` both need numpy>=2 while the previous
    pins hold both below it. Tested one at a time against the old baseline,
    each fails — and both would be reported conflicted. Moving both together
    resolves cleanly, and the floor-relaxation loop gets that for free because
    it raises every aspiration at once.
    """
    catalogue = {
        name: {"1.0": release(name, "1.0"), "2.0": release(name, "2.0", day=20)}
        for name in ("haybale-a", "haybale-b", "haybale-c")
    }
    _stub_pypi(monkeypatch, catalogue)

    def fake_resolve(requirements, constraints, *, exclude_newer, timeout=900.0):
        # Fails only when a and b disagree; both at 2.0 is fine, both at 1.0 is fine.
        a_new = "haybale-a>=2.0" in requirements
        b_new = "haybale-b>=2.0" in requirements
        if a_new != b_new:
            return solve_stable.ResolveOutcome(ok=False, output="haybale-a and haybale-b clash on numpy")
        version = "2.0" if a_new else "1.0"
        return solve_stable.ResolveOutcome(
            ok=True, output="", pins={"haybale-a": version, "haybale-b": version, "haybale-c": "2.0"}
        )

    monkeypatch.setattr(solve_stable, "run_resolve", fake_resolve)

    result = solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=Lock(channel="stable", exclude_newer="", pins={"haybale-a": "1.0", "haybale-b": "1.0"}),
    )
    # Both advanced together; neither is recorded as relaxed.
    assert result.pins["haybale-a"] == "2.0"
    assert result.pins["haybale-b"] == "2.0"
    assert not result.states["haybale-a"].relaxed
    assert not result.states["haybale-b"].relaxed


def test_the_relaxation_leaving_the_most_libraries_newest_wins(registry, monkeypatch):
    """Rule 1, stated directly rather than through a proxy.

    `a` and `b` clash. Both relaxations resolve, so the tiebreak would decide
    if the objective were not applied — and the tiebreak (rule 2) would pick
    the more recently released, which here is `b`. The objective must beat it:
    relaxing `b` drags `c` down to 1.0 as collateral (one library at its
    newest), while relaxing `a` leaves both `b` and `c` current (two). So `a`
    is the correct hold, and it is *not* what the tiebreak alone would choose.
    """
    catalogue = {
        "haybale-a": {"1.0": release("haybale-a", "1.0"), "2.0": release("haybale-a", "2.0", day=5)},
        "haybale-b": {"1.0": release("haybale-b", "1.0"), "2.0": release("haybale-b", "2.0", day=25)},
        "haybale-c": {"1.0": release("haybale-c", "1.0"), "2.0": release("haybale-c", "2.0", day=10)},
    }
    _stub_pypi(monkeypatch, catalogue)

    def fake_resolve(requirements, constraints, *, exclude_newer, timeout=900.0):
        a_new = "haybale-a>=2.0" in requirements
        b_new = "haybale-b>=2.0" in requirements
        c_new = "haybale-c>=2.0" in requirements
        if a_new and b_new:
            return solve_stable.ResolveOutcome(ok=False, output="haybale-a haybale-b clash")
        # `a` at 2.0 caps c at 1.0 — the collateral that makes *keeping* a
        # (i.e. relaxing b) the worse of the two trials.
        pins = {
            "haybale-a": "2.0" if a_new else "1.0",
            "haybale-b": "2.0" if b_new else "1.0",
            "haybale-c": "1.0" if a_new else ("2.0" if c_new else "1.0"),
        }
        return solve_stable.ResolveOutcome(ok=True, output="", pins=pins)

    monkeypatch.setattr(solve_stable, "run_resolve", fake_resolve)

    result = solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=Lock(channel="stable", exclude_newer="", pins={n: "1.0" for n in catalogue}),
    )
    # Relaxing b would have cost c its newest too, so a is the one held —
    # even though rule 2 on its own would have held b.
    assert result.states["haybale-a"].relaxed is True
    assert result.states["haybale-b"].relaxed is False
    assert result.pins["haybale-b"] == "2.0"
    assert result.pins["haybale-c"] == "2.0"


def test_on_a_tie_the_more_recently_released_loses(registry, monkeypatch):
    """Rule 2 — the new release broke the fit, and its author can widen it."""
    catalogue = {
        "haybale-a": {"1.0": release("haybale-a", "1.0"), "2.0": release("haybale-a", "2.0", day=5)},
        "haybale-b": {"1.0": release("haybale-b", "1.0"), "2.0": release("haybale-b", "2.0", day=25)},
        "haybale-c": {"1.0": release("haybale-c", "1.0")},
    }
    _stub_pypi(monkeypatch, catalogue)

    def fake_resolve(requirements, constraints, *, exclude_newer, timeout=900.0):
        # An unbreakable clash: no single relaxation resolves it, which is what
        # forces the tiebreak path.
        if "haybale-a>=2.0" in requirements or "haybale-b>=2.0" in requirements:
            return solve_stable.ResolveOutcome(ok=False, output="haybale-a haybale-b clash")
        return solve_stable.ResolveOutcome(
            ok=True, output="", pins={"haybale-a": "1.0", "haybale-b": "1.0", "haybale-c": "1.0"}
        )

    monkeypatch.setattr(solve_stable, "run_resolve", fake_resolve)

    result = solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=Lock(channel="stable", exclude_newer="", pins={n: "1.0" for n in catalogue}),
    )
    # haybale-b released on the 25th, haybale-a on the 5th: b is relaxed first.
    assert result.states["haybale-b"].relaxed is True


def test_stable_never_regresses_below_its_previous_pin(registry, monkeypatch):
    catalogue = {
        name: {"1.0": release(name, "1.0"), "2.0": release(name, "2.0", day=20)}
        for name in ("haybale-a", "haybale-b", "haybale-c")
    }
    _stub_pypi(monkeypatch, catalogue)

    seen: list[list[str]] = []

    def fake_resolve(requirements, constraints, *, exclude_newer, timeout=900.0):
        seen.append(requirements)
        return solve_stable.ResolveOutcome(ok=True, output="", pins={n: "2.0" for n in catalogue})

    monkeypatch.setattr(solve_stable, "run_resolve", fake_resolve)

    solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=Lock(channel="stable", exclude_newer="", pins={n: "2.0" for n in catalogue}),
    )
    # The floor is the previous pin; even relaxed, it can never go below it.
    for name in catalogue:
        assert solve_stable.LibraryState(name, "2.0", "2.0", {}, relaxed=True).requirement == f"{name}>=2.0"


def test_yanked_releases_are_never_the_aspiration(registry, monkeypatch):
    """Aspiring to a yanked release would defeat the one signal `==` ignores."""
    catalogue = {
        name: {
            "1.0": release(name, "1.0"),
            "2.0": release(name, "2.0", day=20, yanked=True),
        }
        for name in ("haybale-a", "haybale-b", "haybale-c")
    }
    _stub_pypi(monkeypatch, catalogue)
    monkeypatch.setattr(
        solve_stable,
        "run_resolve",
        lambda *a, **k: solve_stable.ResolveOutcome(ok=True, output="", pins={n: "1.0" for n in catalogue}),
    )
    result = solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=None,
    )
    assert all(state.aspiration == "1.0" for state in result.states.values())


# ─────────────────────────────────────────────────────────────────────────────
# The report
# ─────────────────────────────────────────────────────────────────────────────


def test_the_report_explains_every_library_below_its_newest(registry, monkeypatch):
    catalogue = {
        name: {"1.0": release(name, "1.0"), "2.0": release(name, "2.0", day=20)}
        for name in ("haybale-a", "haybale-b", "haybale-c")
    }
    _stub_pypi(monkeypatch, catalogue)

    def fake_resolve(requirements, constraints, *, exclude_newer, timeout=900.0):
        if "haybale-a>=2.0" in requirements:
            return solve_stable.ResolveOutcome(ok=False, output="haybale-a haybale-b clash on numpy")
        return solve_stable.ResolveOutcome(
            ok=True, output="", pins={"haybale-a": "1.0", "haybale-b": "2.0", "haybale-c": "2.0"}
        )

    monkeypatch.setattr(solve_stable, "run_resolve", fake_resolve)

    result = solve(
        repo_root=registry,
        framework={"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"},
        exclude_newer="2026-08-29T00:00:00Z",
        previous=Lock(channel="stable", exclude_newer="", pins={n: "1.0" for n in catalogue}),
    )
    report = render_report(result)

    # Every library whose lock version is below its newest gets an explanation,
    # and it carries the resolver's own derivation — this is the document an
    # author is pointed at.
    assert "haybale-a" in report
    assert "conflict" in report
    assert "clash on numpy" in report
    assert "would have forced" in report
    # A library at its newest needs no explanation.
    assert "### `haybale-c`" not in report
