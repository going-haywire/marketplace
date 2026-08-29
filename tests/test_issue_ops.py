"""Parsing an untrusted issue body into a registry entry.

The body is written by a stranger and the distribution name reaches a file
path, so the name validation here is a security check, not a tidiness one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from issue_to_registry import parse_issue_body, write_entry  # noqa: E402
from marketplace_lib import MarketplaceError, read_registry  # noqa: E402

BODY = """\
### PyPI distribution name

haybale-example-lib

### Source repository

https://github.com/someone/haybale-example-lib

### Contact

someone

### Anything else

_No response_
"""


def test_parses_the_issue_forms_rendered_body():
    submission = parse_issue_body(BODY)
    assert submission.name == "haybale-example-lib"
    assert submission.repo == "https://github.com/someone/haybale-example-lib"
    assert submission.contact == "someone"


def test_an_omitted_optional_field_is_empty_not_the_placeholder():
    # GitHub writes "_No response_" for a skipped optional field; storing that
    # verbatim would put the placeholder into the registry entry.
    body = BODY.replace("someone\n\n### Anything else", "_No response_\n\n### Anything else")
    assert parse_issue_body(body).contact == ""


def test_a_body_from_some_other_template_is_rejected():
    with pytest.raises(MarketplaceError, match="Add a library"):
        parse_issue_body("### Something else\n\nhello\n")


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "haybale-foo; rm -rf /",
        "haybale foo",
        "/abs/path",
        "haybale/foo",
        "$(whoami)",
        "-rf",
        "",
    ],
)
def test_a_hostile_distribution_name_is_rejected_not_sanitised(name):
    """A name is not a path fragment and must not be usable as one.

    Rejected rather than cleaned: a name that needs sanitising is not a name,
    and a sanitiser is a thing to be bypassed.
    """
    body = BODY.replace("haybale-example-lib", name)
    with pytest.raises(MarketplaceError):
        parse_issue_body(body)


def test_writes_a_membership_only_entry(tmp_path):
    submission = parse_issue_body(BODY)
    path = write_entry(submission, tmp_path, issue_number=7)
    text = path.read_text(encoding="utf-8")

    assert path.name == "haybale-example-lib.toml"
    assert 'name    = "haybale-example-lib"' in text

    # Nothing descriptive: label, version, description and tags all come from
    # the wheel at generation time. Checked against the parsed keys rather than
    # the raw text, so the entry's own explanatory comment does not trip it.
    import tomllib

    keys = set(tomllib.loads(text))
    assert keys <= {"name", "repo", "contact", "added", "issue"}, keys

    # And it reads back through the registry reader.
    entries = read_registry(tmp_path)
    assert [e.name for e in entries] == ["haybale-example-lib"]


def test_an_existing_member_is_not_overwritten(tmp_path):
    submission = parse_issue_body(BODY)
    write_entry(submission, tmp_path)
    with pytest.raises(MarketplaceError, match="already in the catalogue"):
        write_entry(submission, tmp_path)
