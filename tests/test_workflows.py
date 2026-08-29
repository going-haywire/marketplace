"""The permission split, asserted against the workflow files themselves.

This is the one property in the design that must be built deliberately rather
than discovered, and it degrades silently: a `secrets` reference added to the
load gate, or an import added to a privileged job, breaks the boundary without
breaking anything visible. So it is checked here, where a regression fails a
test instead of shipping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The workflows that may write, and therefore must never run submitted code.
PRIVILEGED = ("deploy.yml", "nightly.yml", "submit.yml")

#: The one workflow that runs submitted code, and therefore must hold nothing.
UNTRUSTED = "verify.yml"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_every_expected_workflow_exists():
    expected = {"verify.yml", "lock-check.yml", "deploy.yml", "nightly.yml", "submit.yml"}
    assert expected <= {p.name for p in WORKFLOWS.glob("*.yml")}


# ─────────────────────────────────────────────────────────────────────────────
# The untrusted tier
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_declares_no_permissions():
    body = read(UNTRUSTED)
    # Top-level, and on every job: a job-level block overrides the top-level
    # one, so the top-level declaration alone is not sufficient.
    assert re.search(r"^permissions: \{\}", body, re.MULTILINE), "verify.yml needs top-level `permissions: {}`"
    job_permissions = re.findall(r"^    permissions:(.*)$", body, re.MULTILINE)
    assert job_permissions, "each job should declare its own permissions"
    assert all(value.strip() == "{}" for value in job_permissions), job_permissions


def test_verify_references_no_secrets():
    """One secret here hands it to every submitted library."""
    assert "secrets." not in read(UNTRUSTED)


def test_verify_is_not_pull_request_target():
    """`pull_request_target` runs with the base repo's token against fork code.

    That is precisely the combination the split exists to prevent, and it is a
    one-word edit away from `pull_request`.

    Checked against the trigger itself rather than the whole file, so the
    comment in verify.yml explaining *why* it is not used does not trip it.
    """
    triggers = [line.strip().rstrip(":") for line in read(UNTRUSTED).splitlines() if re.match(r"^  \w+:", line)]
    assert "pull_request_target" not in triggers
    assert "pull_request" in triggers


def test_the_load_gate_runs_in_the_untrusted_workflow_only():
    assert "check_load.py" in read(UNTRUSTED)
    for name in PRIVILEGED + ("lock-check.yml",):
        assert "check_load.py" not in read(name), f"{name} can write and must not import submitted code"


# ─────────────────────────────────────────────────────────────────────────────
# The privileged tier
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", PRIVILEGED)
def test_privileged_workflows_never_install_a_candidate(name):
    """Installing is unpacking; the danger is the import that usually follows.

    A privileged job has no reason to `pip install` a submitted library at all,
    so the presence of one is treated as the mistake it almost certainly is.
    """
    body = read(name)
    assert "pip install" not in body, f"{name} installs a package but can write"


@pytest.mark.parametrize("name", PRIVILEGED)
def test_privileged_workflows_declare_their_permissions_explicitly(name):
    body = read(name)
    assert re.search(r"^permissions:", body, re.MULTILINE), f"{name} must declare permissions"


def test_deploy_only_runs_metadata_scripts():
    """Row generation never needs execution — that is what makes the split work."""
    body = read("deploy.yml")
    allowed = {"generate_feed.py", "verify_lock.py"}
    called = set(re.findall(r"scripts/(\w+\.py)", body))
    assert called <= allowed, f"deploy.yml calls unexpected scripts: {called - allowed}"


def test_submit_does_not_interpolate_the_issue_body_into_a_shell():
    """The issue body is attacker-controlled; `${{ }}` in `run:` is injection."""
    body = read("submit.yml")
    assert "${{ github.event.issue.body }}" in body  # passed via env:
    assert "printf '%s' \"$ISSUE_BODY\"" in body
    # It must never appear inside a run: line directly.
    for line in body.splitlines():
        if "github.event.issue.body" in line:
            assert line.strip().startswith("ISSUE_BODY:"), line


def test_nightly_regenerates_edge_only():
    """Rebuilding a pinned channel here would let it drift from its reviewed lock."""
    body = read("nightly.yml")
    assert "--channel edge" in body
