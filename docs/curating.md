# Curating

`CODEOWNERS` puts `registry/`, `stable.constraints.txt` and `locks/` under
`@going-haywire/curators`. Membership is the only judgement call in the system;
everything downstream of it is computed, so keep it that way. Do not hand-edit
a lock or a generated feed to work around a failing check.

## The release ritual

1. The scheduled job opens an issue listing libraries with releases newer than
   the current lock. That is the trigger.
2. Run the solve (below). It completes on its own: read the lock diff and the
   report. Only if you disagree with a relaxation does `stable.constraints.txt`
   come into it ([how-stable-works.md](how-stable-works.md#overriding-the-outcome)).
3. Open a PR with the constraints change, the lock, and the report. CI
   verifies.
4. Merge, tag, deploy.

## Running a curation locally

Work on a branch named `curation/<tag>`, where the tag is the date
(`2026.11.15`).

```sh
# metadata gate: unzips each wheel, imports nothing
uv run python scripts/check_submission.py --all

# solve stable, and write latest alongside it
uv run python scripts/solve_stable.py \
    --framework-version <haywire release> --nicegui <its nicegui> \
    --curation-tag <tag> --latest

# what CI will check
uv run python scripts/verify_lock.py --lock locks/stable.toml
uv run python scripts/verify_lock.py --lock locks/latest.toml --yanks-only
uv run pytest -q

# optional: build the site into a scratch directory and look at it
uv run python scripts/generate_feed.py --out-dir <scratch dir> --curation-tag <tag> --strict
```

With `--curation-tag`, the solve also rewrites the release banner at the top
of `README.md` from the new stable lock. Commit it with the lock:
`verify_lock.py` fails if the banner names a different release than the lock.

The load gate (`scripts/check_load.py`) imports library code. It runs in CI
(`verify.yml`) on Linux, macOS and Windows, in a job with no permissions; it
does not need to run locally.

### Things to know

- **The nicegui version** for `--nicegui` is the one the Haywire release locks:
  `git show v<release>:uv.lock` in the haywire repo, entry `nicegui`.
- **Adding a library** is one file in `registry/`, copied from an existing
  entry. A repo with several `barn/*` libraries publishes several
  distributions; each one needs its own entry.
- **A just-released version can be invisible to the solve** because uv caches
  the package index. If the resolver says a version that PyPI shows does not
  exist, re-run with `UV_NO_CACHE=1`. CI starts with an empty cache and is not
  affected.
- **Re-running at the same instant** (`--exclude-newer <timestamp from the
  lock>`) reproduces a solve exactly. Use it to check that a surprising result
  is not a transient resolver failure.
