# Getting your library listed

Being listed asserts only what the
[README](../README.md#what-being-in-this-catalogue-means--and-does-not) says it
does. Nobody reviews your code.

## Requirements

- **Published on PyPI.** Git-installable libraries are not accepted, from
  anyone, including us. A PyPI version is immutable: it can be deleted or
  yanked, but never replaced with different content. A git tag can be moved.
- **A valid `haybale.toml` inside the wheel**, with at minimum `name` and
  `version`.
- **A `haywire.libraries` entry point**, declared in the library's own
  `pyproject.toml` (`barn/<library>/pyproject.toml` in a project made with
  `haywire init`, which writes it for you).
- **A name not already claimed** by another catalogue entry or by the
  framework's own feed.

## Publishing to PyPI

A project made with `haywire init` already has what it needs: a
`.github/workflows/publish.yml` that builds and uploads to PyPI on every
version tag, using Trusted Publishing (no API tokens). The full walkthrough is
the Haywire guide
[Publishing to PyPI](https://going-haywire.github.io/haywire/docs/guides/publish-to-pypi/).
In short:

1. On PyPI, add a *pending publisher* for **each** library name, and create a
   `pypi` environment in the GitHub repository settings.
2. Declare the distribution mode in the **project root** `pyproject.toml`,
   before the first release:

   ```toml
   [tool.haywire.marketstall]
   distribute = "pypi"
   ```

   This makes your own `marketstall.toml` advertise the same coordinate
   (`haybale-foo==1.2.3`) this catalogue does. Without it your feed advertises
   a git coordinate and this catalogue a pip one, and anyone subscribed to both
   sees your library twice, in conflict.
3. Release the first version with `haywire share --first-release`. The tag push
   starts the workflow, which creates the PyPI project.
4. From then on, release with `haywire share --bump patch`.

## Submitting

Open an issue using the **Add a library** template. Fill in the distribution
name, the source repository, and a contact.

A project that publishes several libraries (several `barn/*` directories) has
several PyPI distributions. Submit each one you want listed: every catalogue
entry is one distribution.

A bot reads the issue, writes the registry entry, runs the checks, and opens a
pull request that closes your issue. The verdict is posted back on your issue,
so if something fails you find out there.

A curator reviews and merges. After that:

- your library appears in **edge** on the next nightly run;
- it appears in **latest** at the next catalogue release, if it loads cleanly;
- it appears in **stable** at the next Haywire release, if it can be fitted
  alongside everything else.

Nobody promotes anything between channels. Placement is computed from the
checks, every time.

## Updating

Nothing to do here. Release as usual (`haywire share --bump patch`). `edge`
picks up your release the next night; the pinned channels pick it up at their
own cadence.

## Why isn't my newest version in `stable`?

Read
[stable-report.md](https://going-haywire.github.io/marketplace/stable-report.md).
It lists every library in the catalogue with its newest release, the version
`stable` currently pins, and, when those differ, which of four reasons applies:

| reason | meaning | what to do |
| --- | --- | --- |
| `conflict` | your version's dependencies cannot be satisfied alongside the rest of the set. The report names the exact clash | widen the constraint if you can, or wait for the other library to move |
| `failed-load` | it installed, then failed to import. The report carries the error | fix and release |
| `held` | a curator deliberately pinned the set below your version. The report says why | read the reason; open an issue if you disagree |
| `not-yet-considered` | you released after the last catalogue run | nothing: the next run will pick it up |

The report is produced by the same command that produces the lock, so it cannot
describe a different set than the one that shipped. How the set is chosen:
[how-stable-works.md](how-stable-works.md).

## Removal

Ask in an issue, and we remove the registry entry. Existing installations keep
working: removal takes the library out of the catalogue, it does not uninstall
anything. Subscribers see the row marked stale on their next refresh.
