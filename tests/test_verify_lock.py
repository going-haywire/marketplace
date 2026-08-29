"""Lock verification: a lock must be something the solver would have produced.

The acceptance criterion this covers — "lock-check fails when locks/stable.toml
is hand-edited" — is not satisfied by asking whether the committed pins still
co-install. An edited pin usually produces a set that resolves perfectly well.
The question that catches it is the stronger one: would the solver have chosen
this? These tests exist because the weaker check was written first and passed a
tampered lock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_lock  # noqa: E402
from marketplace_lib import Lock  # noqa: E402

FRAMEWORK = {"haywire-core": "0.1.2", "haywire-studio": "0.1.2", "nicegui": "3.16.0"}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "registry").mkdir()
    for name in ("haybale-a", "haybale-b"):
        (tmp_path / "registry" / f"{name}.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    (tmp_path / "locks").mkdir()
    return tmp_path


def write_lock(repo: Path, pins: dict[str, str], **kwargs) -> Path:
    lock = Lock(
        channel="stable",
        exclude_newer=kwargs.pop("exclude_newer", "2026-08-29T00:00:00Z"),
        framework=kwargs.pop("framework", dict(FRAMEWORK)),
        pins=pins,
        generated="2026-08-29T00:00:00Z",
    )
    path = repo / "locks" / "stable.toml"
    path.write_text(lock.to_toml(), encoding="utf-8")
    return path


@pytest.fixture
def solver(monkeypatch):
    """Stub the two resolver-backed steps; `truth` is what a fresh solve yields."""
    state = {"truth": {"haybale-a": "2.0", "haybale-b": "3.0"}, "co_installs": True}

    monkeypatch.setattr(
        verify_lock,
        "resolves_together",
        lambda lock, root: (state["co_installs"], "" if state["co_installs"] else "conflict derivation"),
    )
    monkeypatch.setattr(verify_lock, "resolve_from_scratch", lambda lock, root: dict(state["truth"]))
    monkeypatch.setattr(verify_lock, "check_yanks", lambda lock: [])
    return state


def test_a_lock_the_solver_would_produce_passes(repo, solver):
    path = write_lock(repo, {"haybale-a": "2.0", "haybale-b": "3.0"})
    assert verify_lock.verify(path, repo) == []


def test_a_hand_edited_pin_fails(repo, solver):
    """The criterion. An older pin still co-installs, so only the re-solve sees it."""
    path = write_lock(repo, {"haybale-a": "1.0", "haybale-b": "3.0"})
    problems = verify_lock.verify(path, repo)
    assert len(problems) == 1
    assert "haybale-a" in problems[0]
    assert "1.0" in problems[0] and "2.0" in problems[0]
    assert "hand-edit" in problems[0]


def test_a_pin_added_by_hand_fails(repo, solver):
    solver["truth"] = {"haybale-a": "2.0"}
    path = write_lock(repo, {"haybale-a": "2.0", "haybale-b": "3.0"})
    problems = verify_lock.verify(path, repo)
    assert any("leaves it out of the set" in p for p in problems)


def test_a_pin_deleted_by_hand_fails(repo, solver):
    path = write_lock(repo, {"haybale-a": "2.0"})
    problems = verify_lock.verify(path, repo)
    assert any("does not carry it" in p for p in problems)


def test_a_set_that_cannot_co_install_fails(repo, solver):
    solver["co_installs"] = False
    path = write_lock(repo, {"haybale-a": "2.0", "haybale-b": "3.0"})
    problems = verify_lock.verify(path, repo)
    assert any("does not co-install" in p for p in problems)


def test_a_pin_for_a_removed_library_fails(repo, solver):
    """A `git rm` in registry/ must leave the lock too."""
    solver["truth"] = {"haybale-a": "2.0", "haybale-b": "3.0"}
    path = write_lock(repo, {"haybale-a": "2.0", "haybale-b": "3.0", "haybale-gone": "1.0"})
    problems = verify_lock.verify(path, repo)
    assert any("not in registry/" in p for p in problems)


def test_a_lock_without_a_timestamp_is_rejected(repo, solver):
    """The recompile is unsound without it — CI and the curator would see different PyPIs."""
    path = write_lock(repo, {"haybale-a": "2.0"}, exclude_newer="")
    problems = verify_lock.verify(path, repo)
    assert any("exclude_newer" in p for p in problems)


def test_a_lock_missing_a_framework_pin_is_rejected(repo, solver):
    """All three, not just haywire-core: the studio's install path pins all three."""
    path = write_lock(repo, {"haybale-a": "2.0", "haybale-b": "3.0"}, framework={"haywire-core": "0.1.2"})
    problems = verify_lock.verify(path, repo)
    assert any("nicegui" in p and "haywire-studio" in p for p in problems)


def test_the_committed_pins_do_not_seed_their_own_verification(repo, monkeypatch):
    """`resolve_from_scratch` must not take floors from the lock being checked.

    Otherwise an edited pin becomes its own justification — the floor rises to
    the tampered value and the re-solve happily reproduces it.
    """
    captured: dict = {}

    def fake_solve(*, repo_root, framework, exclude_newer, previous, **kwargs):
        captured["previous"] = previous

        class Result:
            pins = {"haybale-a": "2.0"}

        return Result()

    monkeypatch.setattr("solve_stable.solve", fake_solve)
    lock = Lock(
        channel="stable", exclude_newer="2026-08-29T00:00:00Z", framework=dict(FRAMEWORK), pins={"haybale-a": "1.0"}
    )
    verify_lock.resolve_from_scratch(lock, repo)
    assert captured["previous"] is None
