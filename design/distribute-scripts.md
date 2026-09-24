# Design: `distribute.sh` — fan share launch.sh script out to client projects

## Problem

`pyutil` is the source of truth for shared the shell tooling `launch.sh`. Each client project (safeshare,
PowerController, …) needs this script as **a real, committed file** at
`<project>/scripts/launch.sh`, because many things hard-depend on that path.

Today, every edit to a master script is hand-copied into 10+ project repos.
This is manual, error-prone, and easy to forget.

**Important**: This distribute.sh will *only * distribute launch.sh for now. Hard code a variable at the start of distribute.sh to list which scripts  from pyutil are to be copied. Set to just launch.sh for now.

## Decision (agreed)

- **Central bash distributor** living in `pyutil`, run after editing a master.
- **Per-project opt-in** via a `[tool.pyutil]` marker in each project's own
  `pyproject.toml` — no central project list to maintain.
- Copies are **byte-identical** to the master, so drift detection is a trivial
  diff. The "managed" notice lives once inside each *master* script, so every
  copy carries it without the distributor mutating file content.

Rejected: symlinks (dangle on clone/CI; violate "must be a real committed
file"), git submodule/subtree (too heavy for single files), PyPI package
(useful later for CI/other machines, but a release cycle per edit is too heavy
for day-to-day). The distributor is structured so a PyPI `sync` command could
reuse the same copy core later if ever needed.

## Opt-in marker (in each client project's `pyproject.toml`)

```toml
[tool.pyutil]
managed_scripts = ["launch.sh", "servicectrl.sh"]
```

- Absent section / empty list ⇒ project is skipped.
- Each listed name must exist in the pyutil root, else a warning for that entry.
- Scripts always land in `<project>/scripts/` (fixed; keeps the tool simple).

## The script: `pyutil/distribute.sh`

### Discovery
- Dev root defaults to `~/dev`; override with `PYUTIL_DEV_ROOT` env var or
  `--root <dir>`.
- Glob `<root>/*/pyproject.toml`, parse the `[tool.pyutil]` section (reuse the
  section+array parser pattern already proven in the legacy `sync_dev_files.sh`).
- pyutil itself is skipped (never a target of its own distribution).

### Modes / flags
- `distribute.sh` — copy managed scripts into every discovered project.
  Per project, report each script as **new / updated / unchanged**.
- `distribute.sh --check` — **dry run + drift report**. Writes nothing. Lists
  projects whose copy differs from the master (stale *or* locally hand-edited).
  Exits non-zero if any drift is found — a pre-flight to run *before* editing a
  master, so a local hack in some project isn't silently clobbered.
- `distribute.sh --project <path>` — limit to a single project.
- `distribute.sh --root <dir>` — override the dev root.
- `distribute.sh -h|--help`.

### Behavior details
- Overwrite only when content actually differs (keeps mtimes/git noise down).
- `chmod +x` each copied script.
- **Source of truth wins**: a plain `distribute` overwrites project copies. To
  preserve a deliberate local edit, use `--check` first to spot it; local
  divergence is surfaced there, not silently merged.
- The distributor **writes files only** — it never commits. You review and
  commit each client repo yourself (per your git workflow).
- `set -euo pipefail`; clear coloured summary; non-zero exit on any error or
  (in `--check`) any drift.

### "Managed" notice (added once to each master script)
A comment block right after the shebang, e.g.:

```bash
#!/bin/bash
# ─ managed by pyutil ─ do not edit this copy; edit the master in pyutil and run
#   distribute.sh. Local edits will be overwritten. ────────────────────────────
```

Because the notice is in the master, copies stay byte-identical and `--check`
is a straight `sha256`/`cmp` comparison.

## Tests

Per the Python testing rule, drive the bash script from **pytest** integration
tests (subprocess against throwaway fixture trees in `tmp_path`):

- discovery: only projects with a valid `[tool.pyutil]` section are targeted;
  malformed/empty sections skipped.
- copy: requested scripts appear in `<project>/scripts/`, executable, byte-equal
  to master.
- idempotence: second run reports all **unchanged**, writes nothing.
- `--check`: exits non-zero and names the project when a copy is stale or
  hand-edited; exits zero when everything is in sync; writes nothing.
- `--project` / `--root` scoping.
- missing master script named in a marker ⇒ warning, non-zero-ish handling.

Adds a `tests/` dir and a pytest dev dependency to pyutil (currently script-only).

## Docs

- New `README.md` section (no README exists yet): what the tool does, the
  `[tool.pyutil]` marker, and the workflow ("edit master → `./distribute.sh`
  → review & commit each project").

## Open questions for review

1. Script name `distribute.sh` OK, or prefer something like `syncscripts.sh`?
   Nick: Call it `distribute.sh`
2. Dev-root default `~/dev` correct for your machine?
   Nick: Yes
3. Happy with **overwrite-wins + `--check` pre-flight** as the safety model, or
   do you want `distribute` itself to refuse to clobber a locally-edited copy
   without `--force`?
   Nick: The latter
4. OK to add a pytest `tests/` setup to pyutil for this?
   Nick: Yes

## Final implementation notes (as built)

These record where the build refined the plan above:

- **Rollout allowlist.** `distribute.sh` has a hard-coded `MANAGED_SCRIPTS`
  array at the top, currently `("launch.sh")`. The effective set per project is
  the intersection of that allowlist with the project's `[tool.pyutil]
  managed_scripts`. A project may list a script that isn't allowlisted yet; it's
  skipped until the allowlist grows.

- **Provenance header instead of byte-identical copies.** To honour the chosen
  safety model (refuse to clobber a *locally-edited* copy without `--force`,
  while still refreshing a merely *stale* one), the distributor injects a small
  header after the shebang carrying the source sha256. This lets it distinguish,
  with no external state file:
  - **up to date** (copy == current master) → skip;
  - **stale** (untouched, from an older master) → refresh;
  - **edited / unknown provenance** (body changed since we wrote it, or no
    header and != master) → refuse without `--force`;
  - **adopt** (unstamped but identical to master) → stamp in place.

- **Exit codes.** `--check` exits 1 on any drift; a normal run exits 1 if any
  copy was refused (local edit) or a listed master was missing.

- **Tests** live in `tests/test_distribute.py`, driving the real script as a
  subprocess against `tmp_path` fixtures (pytest). A minimal root
  `pyproject.toml` adds the `pytest` dev dependency.
