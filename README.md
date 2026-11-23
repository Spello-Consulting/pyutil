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

## `deploy_check.sh` — check a host is ready for auto-deployment

Checks that the host it runs on, and the project in the current folder, are set
up for auto-deployment as described in the
[deployment guides](https://spello-consulting.github.io/automation/deployment/).
Run the deployed copy from the project's folder:

```bash
cd ~/scripts/PowerController
~/scripts/pyutil/deploy_check.sh
```

Each check prints `PASS`, `WARN`, `FAIL` or `SKIP`, and anything that isn't a
pass says what to do about it. The exit status is `0` if nothing failed
(warnings are allowed) and `1` otherwise. The script is **read-only**: it never
changes anything, and it reads remotes with `git ls-remote`, so even the clones
are left untouched.

What it checks:

- **Every host** — `git` and `curl` are installed; `~/scripts/pyutil` is a clone
  of the org repo, on `main`, matching `origin/main`, with no local changes, and
  owned entirely by the deploy user; pyutil's own `deploy.yml` has a job for
  this host; the runner is registered with the organisation under the host's
  name (and, if `gh` can see the org's runners, is online with that label).
- **Linux** — the `github-runner` user exists with a locked password; the
  runner's systemd service is enabled, running and runs as `github-runner`;
  uv is in `~/.local/bin` and linked from `/usr/local/bin/uv`; pyutil is a git
  `safe.directory`; `/etc/sudoers.d/github-runner` is valid and allows every
  `sudo` call the deploy workflows make (with the line to add if not), and has
  no unused legacy `git pull` lines.
- **macOS** — Homebrew, `git` and `uv` are installed; the runner's LaunchAgent
  is loaded and running; the runner's saved PATH (`.path`) includes uv and git.
- **The project** — it is in `~/scripts/<Project>` with an org `origin` that can
  be read without a password; every file is owned by the deploy user; it is on
  `main` with no local changes; `.env.target` links to a production template
  and the `APP_CONFIG` file it names exists; `deploy.yml` has this host as an
  option and a job (or matrix entry) for it with `environment: <Host>`; on
  Linux, the job's `sudo` calls are allowed; the systemd unit templates
  (`deploy/<Host>/<service>.service`) or LaunchAgent plist
  (`deploy/<user_agent_name>.plist`) exist; `deploy/auto-deploy.yaml` passes the
  same rules the auto-deploy orchestrator applies; and the GitHub Environment
  exists (if `gh` is available).

The runner folder is found from its systemd unit or LaunchAgent, so runners
installed somewhere other than the guide's location are still checked. The
host name comes from the runner's registration, else `hostname -s`.

Options:

| Flag            | Effect                                                              |
| --------------- | ------------------------------------------------------------------- |
| `--project DIR` | Check this project folder instead of the current one.               |
| `--host HOST`   | Host name / runner label to check for.                              |
| `--host-only`   | Only check the host.                                                |
| `--offline`     | Skip checks that use the network (`git ls-remote`, `gh`).           |
| `--no-sudo`     | Never use sudo; root-only checks become warnings.                   |
| `--no-color`    | Plain output.                                                       |

On Linux some files (the sudoers file, the runner's folder) can only be read by
root. If `sudo` needs a password, the script asks for it once up front (like
`sudo -v`); press Ctrl-C and use `--no-sudo` to skip those checks.

`deploy_check.sh` is a small wrapper that finds uv and runs `deploy_check.py`
with the dependencies declared in that file (PyYAML), so nothing needs to be
installed first.

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
uv run pytest                                                   # test suite (tests/)
uv run ruff check .                                             # lint (uses ~/.config/ruff/ruff.toml)
uv run mypy --strict deploy_check.py tests/test_deploy_check.py # type check
```

