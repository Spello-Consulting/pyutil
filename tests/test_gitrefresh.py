"""Integration tests for ``gitrefresh.sh``.

The refresh script is bash, so these tests drive it as a subprocess against a
throwaway bare "origin" repo and a deployed clone of it. Running as root is
simulated with stub commands placed first on ``PATH``: ``id`` reports uid 0,
``sudo`` records its arguments and then runs the command as the current user,
``stat`` can report a different owner for the clone, and ``chown`` and ``uv``
only record that they were called. Assertions are made on the exit status, the
recorded calls and the state of the deployed clone.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"
ME = getpass.getuser()

PYPROJECT = '[project]\nname = "demo"\nversion = "1.0.0"\n'

# Each stub appends one line, "<name> <args...>", to $FAKE_LOG.
FAKE_SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --) shift; break ;;
    -u) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
[[ "$(basename "$1")" == "servicectrl.sh" ]] && exit 0
exec "$@"
"""
FAKE_ID = """#!/usr/bin/env bash
if [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi
"""
FAKE_LOGGER = """#!/usr/bin/env bash
echo "{name} $*" >> "$FAKE_LOG"
"""


def _write(path: Path, content: str, *, executable: bool = False) -> Path:
    """Write ``content`` to ``path`` as UTF-8, creating parent directories.

    Args:
        path: Destination file path.
        content: Text to write.
        executable: Whether to mark the file executable.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def git(cwd: Path, env: dict[str, str], *args: str) -> str:
    """Run a git command and return its stripped stdout.

    Args:
        cwd: Directory to run git in.
        env: Environment to run git with.
        *args: Arguments passed to git.

    Returns:
        The command's stdout, stripped of surrounding whitespace.
    """
    result = subprocess.run(  # noqa: S603
        [GIT, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """Create a stub bin dir holding the ``sudo`` and ``uv`` stubs.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Path to the stub bin dir.
    """
    bin_dir = tmp_path / "fakebin"
    _write(bin_dir / "sudo", FAKE_SUDO, executable=True)
    _write(bin_dir / "uv", FAKE_LOGGER.format(name="uv"), executable=True)
    return bin_dir


@pytest.fixture
def env(tmp_path: Path, fake_bin: Path) -> dict[str, str]:
    """Build an environment isolated from the user's git config, stubs first on PATH.

    Args:
        tmp_path: pytest-provided temporary directory.
        fake_bin: Stub bin dir.

    Returns:
        Environment variables for running git and the refresh script.
    """
    gitconfig = _write(
        tmp_path / "gitconfig",
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n",
    )
    result = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result.update(
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        FAKE_LOG=str(tmp_path / "calls.log"),
    )
    return result


@pytest.fixture
def scripts_dir(tmp_path: Path) -> Path:
    """Copy the real refresh script and its dev-check library into an isolated dir.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Directory holding ``gitrefresh.sh``.
    """
    root = tmp_path / "pyutil"
    root.mkdir()
    for name in ("gitrefresh.sh", "git_dev_env_check.sh"):
        shutil.copy(REPO_ROOT / name, root / name)
    return root


@pytest.fixture
def deployed(tmp_path: Path, env: dict[str, str]) -> tuple[Path, str]:
    """Create an origin, a deployed clone, and push a newer commit to origin.

    Args:
        tmp_path: pytest-provided temporary directory.
        env: Isolated git environment.

    Returns:
        The deployed clone (one commit behind origin) and origin's latest commit.
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, env, "init", "--bare", "--initial-branch=main", str(origin))

    work = tmp_path / "work"
    git(tmp_path, env, "clone", str(origin), str(work))
    _write(work / "pyproject.toml", PYPROJECT)
    git(work, env, "add", ".")
    git(work, env, "commit", "-m", "first")
    git(work, env, "push", "origin", "main")

    clone = tmp_path / "scripts" / "demo"
    git(tmp_path, env, "clone", str(origin), str(clone))

    _write(work / "app.py", "print('v2')\n")
    git(work, env, "add", ".")
    git(work, env, "commit", "-m", "second")
    git(work, env, "push", "origin", "main")
    return clone, git(work, env, "rev-parse", "HEAD")


def _simulate_root(fake_bin: Path, owner_group: str | None = None) -> None:
    """Make the script believe it runs as root, optionally with a given clone owner.

    Args:
        fake_bin: Stub bin dir.
        owner_group: If set, ``stat`` reports the clone as owned by this
            ``user:group``, and ``chown`` is stubbed so nothing really changes.
    """
    _write(fake_bin / "id", FAKE_ID, executable=True)
    if owner_group is not None:
        _write(fake_bin / "stat", f"#!/usr/bin/env bash\necho {owner_group}\n", executable=True)
        _write(fake_bin / "chown", FAKE_LOGGER.format(name="chown"), executable=True)


def run_refresh(
    scripts_dir: Path, clone: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run ``gitrefresh.sh --yes`` in ``clone``.

    Args:
        scripts_dir: Directory holding the copied refresh script.
        clone: Deployed clone to refresh.
        env: Environment to run the script with.

    Returns:
        The completed process, and the calls recorded by the stubs.
    """
    result = subprocess.run(  # noqa: S603
        [BASH, str(scripts_dir / "gitrefresh.sh"), "--yes"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    log = Path(env["FAKE_LOG"])
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_not_root_runs_git_and_uv_directly(
    scripts_dir: Path, deployed: tuple[Path, str], env: dict[str, str]
) -> None:
    """Run as a normal user, the refresh uses no sudo and syncs with uv directly."""
    clone, latest = deployed

    result, calls = run_refresh(scripts_dir, clone, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(clone, env, "rev-parse", "HEAD") == latest
    assert calls == ["uv sync"]


def test_root_runs_git_and_uv_as_clone_owner(
    scripts_dir: Path, deployed: tuple[Path, str], env: dict[str, str], fake_bin: Path
) -> None:
    """Run as root, git and uv run as the clone's owner, and nothing is chowned."""
    clone, latest = deployed
    _simulate_root(fake_bin)

    result, calls = run_refresh(scripts_dir, clone, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(clone, env, "rev-parse", "HEAD") == latest
    prefix = f"sudo -n -u {ME} -H -- "
    assert f"{prefix}git fetch origin main --tags --force" in calls
    assert f"{prefix}git reset --hard origin/main" in calls
    assert f"{prefix}{fake_bin / 'uv'} sync" in calls
    assert not any(c.startswith("chown") for c in calls)
    assert f"git and uv will run as the clone's owner, '{ME}'" in result.stdout


def test_root_fixes_ownership_before_refresh(
    scripts_dir: Path, deployed: tuple[Path, str], env: dict[str, str], fake_bin: Path
) -> None:
    """Run as root, files not owned by the clone's owner are chowned back first."""
    clone, latest = deployed
    _simulate_root(fake_bin, owner_group="nobody:nogroup")

    result, calls = run_refresh(scripts_dir, clone, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(clone, env, "rev-parse", "HEAD") == latest
    chown_call = f"chown -R -h nobody:nogroup {clone.resolve()}"
    assert chown_call in calls
    fetch_call = "sudo -n -u nobody -H -- git fetch origin main --tags --force"
    assert calls.index(chown_call) < calls.index(fetch_call)
    assert "not owned by 'nobody'" in result.stdout


def test_root_owned_clone_runs_git_as_root(
    scripts_dir: Path, deployed: tuple[Path, str], env: dict[str, str], fake_bin: Path
) -> None:
    """Run as root on a clone that root owns, git runs directly with no sudo."""
    clone, latest = deployed
    _simulate_root(fake_bin, owner_group="root:root")

    result, calls = run_refresh(scripts_dir, clone, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(clone, env, "rev-parse", "HEAD") == latest
    assert calls == ["uv sync"]
