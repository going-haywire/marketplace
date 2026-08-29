"""The load gate: does this library actually import and register?

    uv run python scripts/check_load.py --registry registry
    uv run python scripts/check_load.py --spec haybale-foo==1.2.3

**This script imports submitted code, and is the only one that does.** It runs
exclusively in `verify.yml`, which has `permissions: {}`, no secrets, and a
`pull_request` trigger. Do not call it from a workflow that can write anything:
installing a wheel executes nothing, but importing it runs whatever the author
put at module scope, and a job holding a write token is a job worth attacking.

Each library is installed and imported in its **own** environment, one at a
time. That is deliberate: this gate answers "does this library load on its
own", which is exactly what the `latest` channel asserts. Whether a set loads
*together* is a different question, and the only thing that can answer it is a
resolve over the whole set — which is what `stable` is.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketplace_lib import (  # noqa: E402
    ENTRY_POINT_GROUP,
    MarketplaceError,
    fetch_all_releases,
    newest_version,
    read_lock,
    read_registry,
)

#: Run inside the throwaway environment, never in this process. Importing a
#: submitted library into the checker itself would mean a library could tamper
#: with the checker's own verdict.
_PROBE = f"""
import json, sys
from importlib.metadata import entry_points

name = sys.argv[1]
result = {{"name": name, "ok": False, "error": "", "components": []}}
try:
    found = [ep for ep in entry_points(group={ENTRY_POINT_GROUP!r})]
    mine = [ep for ep in found if ep.dist and ep.dist.name.replace("_", "-").lower() == name.lower()]
    if not mine:
        result["error"] = (
            "installed, but declares no {ENTRY_POINT_GROUP} entry point — "
            "a studio would never load it"
        )
    else:
        for ep in mine:
            # The import: this is the line the permission split exists for.
            loaded = ep.load()
            result["components"].append(f"{{ep.name}} -> {{ep.value}}")
            if loaded is None:
                raise RuntimeError(f"entry point {{ep.name}} loaded as None")
        result["ok"] = True
except BaseException as exc:
    result["error"] = f"{{type(exc).__name__}}: {{exc}}"

print("HAYWIRE_LOAD_RESULT " + json.dumps(result))
"""


@dataclass
class LoadResult:
    """One library's verdict from the load gate."""

    name: str
    spec: str
    ok: bool
    skipped: bool = False
    error: str = ""
    components: list[str] | None = None


def _runner_platform() -> str:
    """This runner, in the vocabulary a haybale's `os` field uses."""
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def check_load(spec: str, name: str, *, declared_os: list[str] | None = None, timeout: float = 1800.0) -> LoadResult:
    """Install *spec* into a throwaway environment and import it there.

    A library whose `os` field excludes this runner is skipped, not failed: the
    author declared it does not run here, and a Linux-only library failing the
    macOS job would be a true statement reported as a defect.
    """
    platform = _runner_platform()
    if declared_os and platform not in declared_os:
        return LoadResult(name=name, spec=spec, ok=True, skipped=True, error=f"declares os = {declared_os}")

    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        try:
            subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, text=True, timeout=300)
        except FileNotFoundError:
            raise MarketplaceError("`uv` is not on PATH") from None
        except subprocess.CalledProcessError as exc:
            return LoadResult(name=name, spec=spec, ok=False, error=f"could not create a venv: {exc.stderr}")

        python = venv / ("Scripts" if sys.platform.startswith("win") else "bin") / "python"

        install = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), spec],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if install.returncode != 0:
            # Installing is resolution and unpacking — no code has run yet.
            return LoadResult(name=name, spec=spec, ok=False, error=f"install failed:\n{install.stderr.strip()}")

        probe = Path(tmp) / "probe.py"
        probe.write_text(_PROBE, encoding="utf-8")
        run = subprocess.run(
            [str(python), str(probe), name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    marker = "HAYWIRE_LOAD_RESULT "
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith(marker)), None)
    if line is None:
        # The probe did not finish — a crash, an exit(), or a hang killed at
        # module scope. That is a load failure, and a loud one.
        return LoadResult(
            name=name,
            spec=spec,
            ok=False,
            error=f"the import did not complete (exit {run.returncode}):\n{(run.stderr or run.stdout).strip()[:2000]}",
        )

    payload = json.loads(line[len(marker) :])
    return LoadResult(
        name=name,
        spec=spec,
        ok=bool(payload["ok"]),
        error=str(payload["error"]),
        components=list(payload["components"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_load", description="The load gate (untrusted tier).")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--registry", type=Path, default=None, help="Check every entry at its newest release.")
    parser.add_argument("--lock", type=Path, default=None, help="Check every pin in this lock instead.")
    parser.add_argument("--spec", action="append", help="Check this exact spec (repeatable).")
    args = parser.parse_args(argv)

    targets: list[tuple[str, str]] = []  # (name, spec)

    if args.lock:
        lock = read_lock(args.lock)
        targets = [(name, f"{name}=={version}") for name, version in sorted(lock.pins.items())]
    elif args.registry:
        for entry in read_registry(args.registry):
            try:
                version = newest_version(fetch_all_releases(entry.name))
            except MarketplaceError as exc:
                print(f"FAIL {entry.name}: {exc}", file=sys.stderr)
                return 1
            targets.append((entry.name, f"{entry.name}=={version}"))
    elif args.spec:
        targets = [(spec.split("==")[0].split(">")[0].split("<")[0].strip(), spec) for spec in args.spec]
    else:
        parser.error("pass --registry, --lock or --spec")

    failed = False
    for name, spec in targets:
        result = check_load(spec, name)
        if result.skipped:
            print(f"SKIP {spec} — {result.error}", file=sys.stderr)
        elif result.ok:
            print(f"OK   {spec} — {', '.join(result.components or []) or 'loaded'}", file=sys.stderr)
        else:
            failed = True
            print(f"FAIL {spec}\n{result.error}\n", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
