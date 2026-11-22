# pyutil

Shared shell tooling and dev-environment scripts, used across my Python
projects. `pyutil` is the **source of truth** for these scripts; client
projects receive copies.

## `distribute.sh` — keep shared scripts in sync

Some scripts (currently just `launch.sh`) must live as real, committed files at
`<project>/scripts/<name>` in each client project, because many things depend on
that exact path. `distribute.sh` copies the master versions here out to those
projects so a change only has to be made once.

### How a project opts in

Add a marker to the client project's own `pyproject.toml`:

```toml
[tool.pyutil]
managed_scripts = ["launch.sh"]
```

`distribute.sh` scans the dev root (default `~/dev`), and for every project with
this marker copies the requested scripts into `<project>/scripts/`. Only scripts
on the distributor's internal allowlist are ever copied — see
`MANAGED_SCRIPTS` at the top of `distribute.sh` (currently `launch.sh` only).
A project may list a script that isn't allowlisted yet; it's simply skipped
until the allowlist catches up.

### Everyday use

Edit the master here, then fan it out:

```bash
./distribute.sh
```

Preview what would change without writing anything (exits non-zero if anything
is out of date — handy before you start editing a master):

```bash
./distribute.sh --check
```

Other flags:

| Flag             | Effect                                                        |
| ---------------- | ------------------------------------------------------------- |
| `--check`        | Dry run: report drift, write nothing, non-zero exit on drift. |
| `--force`        | Overwrite copies that were edited locally / are unstamped.    |
| `--project DIR`  | Limit to a single project directory.                          |
| `--root DIR`     | Dev root to scan (default: `$PYUTIL_DEV_ROOT` or `~/dev`).     |
| `-h`, `--help`   | Show usage.                                                   |

The distributor **only writes files** — it never commits. Review and commit
each client repo yourself.

### How it protects local edits

Each distributed copy carries a small provenance header (a "managed by pyutil"
notice plus the source hash) injected after the shebang:

```bash
#!/bin/bash
# >>>>> pyutil-managed >>>>>
# Managed by pyutil — do not edit this copy.
# Source of truth: pyutil/launch.sh — edit there, then run pyutil/distribute.sh.
# Updates refuse to overwrite local edits unless --force is used.
# pyutil-source-sha256: <hash>
# <<<<< pyutil-managed <<<<<
...
```

Using that header, `distribute.sh` tells three situations apart:

- **up to date** — copy matches the current master; skipped.
- **stale** — copy is an untouched older version; refreshed automatically.
- **local edit / unknown provenance** — copy was hand-edited (or has no header
  and doesn't match the master); **refused** unless you pass `--force`.

So editing a project's copy directly is safe from silent clobbering — the next
`distribute.sh` will refuse it and tell you, rather than overwrite it.

## `release.sh` — cut a release

Run from the root of the project being released, with its virtual environment
activated:

```bash
~/dev/pyutil/release.sh [--force] <version> <comment>
```

It runs the tests (if there is a `tests/` folder) and builds the docs (if there
is a `mkdocs.yml`), sets `version` in the `[project]` table of `pyproject.toml`,
commits all changes with `<comment>`, tags the commit `v<version>`, pushes
`main` and the tag, and finally deploys the docs to gh-pages. Projects without a
`pyproject.toml` are handed off to `nopy_release.sh`, which does the same
commit / tag / push using git tags alone for versioning.

Things worth knowing:

- `<version>` may be given as `1.2.3` or `v1.2.3`; the tag is always `v1.2.3`.
- It's fine if `pyproject.toml` is already at `<version>` (e.g. bumped by
  hand) — the release is still tagged.
- If there's nothing left to commit, the current `HEAD` is tagged.
- Before changing anything it checks that you're on `main` and that the tag
  doesn't already exist locally or on `origin`; any failed step stops the
  script with an error rather than carrying on.
- To re-release a version that's already tagged (e.g. to push a very minor
  change without a version bump), pass `--force` (or `-f`). The changes are
  committed and pushed to `main`, and the existing tag is left where it is —
  it is never moved. If the tag exists locally but was never pushed, it is
  pushed as is.

## Development

```bash
uv run pytest          # test suite (tests/)
uv run ruff check .    # lint (uses ~/.config/ruff/ruff.toml)
```

