"""Integration tests for ``release.sh`` and ``nopy_release.sh``.

Both release scripts are bash, so these tests drive them as subprocesses
against throwaway git repos. Each test gets a bare "origin" repo plus a clone
of it to release from, with git configured in isolation from the user's own
global and system config. Assertions are made on the exit status, the
script output and the resulting state of the local and remote repos.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"

PYPROJECT = '[project]\nname = "demo"\nversion = "{version}"\n'


def _write(path: Path, content: str) -> Path:
    """Write ``content`` to ``path`` as UTF-8, creating parent directories.

    Args:
        path: Destination file path.
        content: Text to write.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Build an environment that isolates git from the user's own config.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Environment variables for running git and the release scripts.
    """
    gitconfig = _write(
        tmp_path / "gitconfig",
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n"
        "[tag]\n\tgpgsign = false\n",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        VIRTUAL_ENV=str(tmp_path / "venv"),
    )
    return env


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
def scripts_dir(tmp_path: Path) -> Path:
    """Copy the real release scripts into an isolated directory.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Directory holding ``release.sh`` and ``nopy_release.sh``.
    """
    root = tmp_path / "pyutil"
    root.mkdir()
    for name in ("release.sh", "nopy_release.sh"):
        shutil.copy(REPO_ROOT / name, root / name)
    return root


@pytest.fixture
def origin(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """Create an empty bare repo to act as ``origin``.

    Args:
        tmp_path: pytest-provided temporary directory.
        git_env: Isolated git environment.

    Returns:
        Path to the bare repo.
    """
    path = tmp_path / "origin.git"
    git(tmp_path, git_env, "init", "--bare", "--initial-branch=main", str(path))
    return path


def make_project(
    tmp_path: Path,
    origin: Path,
    env: dict[str, str],
    pyproject: str | None,
) -> Path:
    """Clone ``origin`` and push an initial commit to its ``main`` branch.

    Args:
        tmp_path: pytest-provided temporary directory.
        origin: Bare repo to clone from and push to.
        env: Isolated git environment.
        pyproject: Contents of ``pyproject.toml``, or ``None`` for a
            non-Python project.

    Returns:
        Path to the working clone.
    """
    project = tmp_path / "project"
    git(tmp_path, env, "clone", "--quiet", str(origin), str(project))
    _write(project / "README.md", "demo\n")
    if pyproject is not None:
        _write(project / "pyproject.toml", pyproject)
    git(project, env, "add", ".")
    git(project, env, "commit", "--quiet", "-m", "initial")
    git(project, env, "push", "--quiet", "origin", "main")
    return project


@pytest.fixture
def py_project(tmp_path: Path, origin: Path, git_env: dict[str, str]) -> Path:
    """Create a Python project at version 1.0.0, pushed to ``origin``.

    Args:
        tmp_path: pytest-provided temporary directory.
        origin: Bare repo acting as origin.
        git_env: Isolated git environment.

    Returns:
        Path to the working clone.
    """
    return make_project(tmp_path, origin, git_env, PYPROJECT.format(version="1.0.0"))


@pytest.fixture
def nopy_project(tmp_path: Path, origin: Path, git_env: dict[str, str]) -> Path:
    """Create a non-Python project tagged v1.0.0, pushed to ``origin``.

    Args:
        tmp_path: pytest-provided temporary directory.
        origin: Bare repo acting as origin.
        git_env: Isolated git environment.

    Returns:
        Path to the working clone.
    """
    project = make_project(tmp_path, origin, git_env, None)
    git(project, git_env, "tag", "v1.0.0")
    git(project, git_env, "push", "--quiet", "origin", "v1.0.0")
    return project


def run(
    script: Path,
    project: Path,
    env: dict[str, str],
    *args: str,
    answer: str = "Y",
) -> subprocess.CompletedProcess[str]:
    """Invoke a release script in ``project`` and capture the result.

    Args:
        script: Release script to run.
        project: Working clone to release from.
        env: Isolated git environment.
        *args: Command-line arguments passed to the script.
        answer: Reply given to the confirmation prompt.

    Returns:
        The completed process, with stdout/stderr captured as text.
    """
    return subprocess.run(  # noqa: S603
        [BASH, str(script), *args],
        cwd=project,
        env=env,
        input=f"{answer}\n",
        capture_output=True,
        text=True,
        check=False,
    )


def remote_tag_commit(origin: Path, env: dict[str, str], tag: str) -> str:
    """Return the commit a tag points to on ``origin``, or ``""`` if absent.

    Args:
        origin: Bare repo acting as origin.
        env: Isolated git environment.
        tag: Tag name to look up.

    Returns:
        The commit SHA, or an empty string if the tag does not exist.
    """
    return git(origin, env, "tag", "--list", tag, "--format=%(objectname)")


def reject_tag_pushes(origin: Path) -> None:
    """Install a hook on ``origin`` that rejects any pushed tag.

    Args:
        origin: Bare repo acting as origin.
    """
    hook = _write(
        origin / "hooks" / "pre-receive",
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        '  case "$ref" in refs/tags/*) echo "tags rejected" >&2; exit 1;; esac\n'
        "done\n",
    )
    hook.chmod(0o755)


def mark_released(project: Path, env: dict[str, str], *, push_tag: bool) -> str:
    """Set the project to version 1.0.1 and tag it, as a prior release would.

    Args:
        project: Working clone to release from.
        env: Isolated git environment.
        push_tag: Whether to push the ``v1.0.1`` tag (and ``main``) to origin.

    Returns:
        The commit the ``v1.0.1`` tag points to.
    """
    _write(project / "pyproject.toml", PYPROJECT.format(version="1.0.1"))
    git(project, env, "commit", "--quiet", "-am", "Release 1.0.1")
    git(project, env, "tag", "v1.0.1")
    if push_tag:
        git(project, env, "push", "--quiet", "origin", "main", "v1.0.1")
    return git(project, env, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# release.sh
# --------------------------------------------------------------------------


def test_release_bumps_version_tags_and_pushes(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """A normal release bumps pyproject.toml and pushes the commit and tag."""
    _write(py_project / "feature.txt", "new\n")
    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Feature")

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'version = "1.0.1"' in (py_project / "pyproject.toml").read_text()
    head = git(py_project, git_env, "rev-parse", "HEAD")
    assert git(origin, git_env, "rev-parse", "main") == head
    assert remote_tag_commit(origin, git_env, "v1.0.1") == head
    assert "Release v1.0.1 committed and pushed" in result.stdout


def test_release_tags_when_version_already_bumped(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """Regression for issue 17: a pre-bumped version must still be tagged."""
    _write(py_project / "pyproject.toml", PYPROJECT.format(version="1.0.1"))
    git(py_project, git_env, "commit", "--quiet", "-am", "Bump by hand")
    _write(py_project / "feature.txt", "new\n")

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Feature")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already at this version" in result.stdout
    head = git(py_project, git_env, "rev-parse", "HEAD")
    assert remote_tag_commit(origin, git_env, "v1.0.1") == head


def test_release_with_nothing_to_commit_tags_head(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """With a pre-bumped, fully committed tree, HEAD is tagged and pushed."""
    _write(py_project / "pyproject.toml", PYPROJECT.format(version="1.0.1"))
    git(py_project, git_env, "commit", "--quiet", "-am", "Bump by hand")
    head = git(py_project, git_env, "rev-parse", "HEAD")

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Release")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Nothing to commit" in result.stdout
    assert git(py_project, git_env, "rev-parse", "HEAD") == head
    assert remote_tag_commit(origin, git_env, "v1.0.1") == head


@pytest.mark.parametrize("where", ["local", "origin"])
def test_release_refuses_existing_tag_before_changing_anything(
    scripts_dir: Path,
    py_project: Path,
    git_env: dict[str, str],
    where: str,
) -> None:
    """An existing tag is reported up front, with no commit or version bump."""
    git(py_project, git_env, "tag", "v1.0.1")
    if where == "origin":
        git(py_project, git_env, "push", "--quiet", "origin", "v1.0.1")
        git(py_project, git_env, "tag", "-d", "v1.0.1")
    head = git(py_project, git_env, "rev-parse", "HEAD")

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Again")

    assert result.returncode == 1
    assert (
        f"tag v1.0.1 already exists {'locally' if where == 'local' else 'on origin'}"
        in result.stdout
    )
    assert git(py_project, git_env, "rev-parse", "HEAD") == head
    assert 'version = "1.0.0"' in (py_project / "pyproject.toml").read_text()


def test_release_reports_failed_tag_push(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """A rejected tag push makes the script fail instead of reporting success."""
    reject_tag_pushes(origin)
    _write(py_project / "feature.txt", "new\n")

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Feature")

    assert result.returncode == 1
    assert "Error: git push origin v1.0.1 failed" in result.stdout
    assert "committed and pushed" not in result.stdout


def test_release_accepts_v_prefix(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """``v1.0.1`` is treated as ``1.0.1`` rather than tagging ``vv1.0.1``."""
    result = run(scripts_dir / "release.sh", py_project, git_env, "v1.0.1", "Release")

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'version = "1.0.1"' in (py_project / "pyproject.toml").read_text()
    assert remote_tag_commit(origin, git_env, "v1.0.1")
    assert not remote_tag_commit(origin, git_env, "vv1.0.1")


def test_release_only_updates_project_version(
    scripts_dir: Path,
    py_project: Path,
    git_env: dict[str, str],
) -> None:
    """Only ``version`` in the ``[project]`` table is rewritten."""
    _write(
        py_project / "pyproject.toml",
        PYPROJECT.format(version="1.0.0") + '\n[tool.other]\nversion = "9.9.9"\n',
    )
    git(py_project, git_env, "commit", "--quiet", "-am", "Add tool table")

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Release")

    assert result.returncode == 0, result.stdout + result.stderr
    text = (py_project / "pyproject.toml").read_text()
    assert 'version = "1.0.1"' in text
    assert 'version = "9.9.9"' in text
    assert not (py_project / "pyproject.toml.bak").exists()


def test_release_refuses_non_main_branch(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """Releasing from a branch other than ``main`` is refused."""
    git(py_project, git_env, "checkout", "--quiet", "-b", "feature")

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Release")

    assert result.returncode == 1
    assert "releases must be made from 'main'" in result.stdout
    assert not remote_tag_commit(origin, git_env, "v1.0.1")


def test_release_abort_changes_nothing(
    scripts_dir: Path,
    py_project: Path,
    git_env: dict[str, str],
) -> None:
    """Declining the confirmation prompt leaves the repo untouched."""
    head = git(py_project, git_env, "rev-parse", "HEAD")

    result = run(
        scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Release", answer="n"
    )

    assert result.returncode == 0
    assert "Aborted." in result.stdout
    assert git(py_project, git_env, "rev-parse", "HEAD") == head
    assert not git(py_project, git_env, "tag", "--list")
    assert 'version = "1.0.0"' in (py_project / "pyproject.toml").read_text()


def test_release_hands_off_to_nopy_release(
    scripts_dir: Path,
    nopy_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """Without pyproject.toml, release.sh runs nopy_release.sh instead."""
    result = run(scripts_dir / "release.sh", nopy_project, git_env, "1.0.1", "Release")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Current version: v1.0.0" in result.stdout
    assert remote_tag_commit(origin, git_env, "v1.0.1")


def test_release_existing_tag_error_suggests_force(
    scripts_dir: Path,
    py_project: Path,
    git_env: dict[str, str],
) -> None:
    """The existing-tag error tells the user about ``--force``."""
    mark_released(py_project, git_env, push_tag=True)

    result = run(scripts_dir / "release.sh", py_project, git_env, "1.0.1", "Tweak")

    assert result.returncode == 1
    assert "use --force to re-release v1.0.1" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("--force", "1.0.1", "Tweak"),
        ("1.0.1", "Tweak", "-f"),
    ],
)
def test_release_force_rereleases_without_moving_tag(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
    args: tuple[str, ...],
) -> None:
    """``--force``/``-f`` pushes new changes and leaves the existing tag alone."""
    tagged = mark_released(py_project, git_env, push_tag=True)
    _write(py_project / "tweak.txt", "minor\n")

    result = run(scripts_dir / "release.sh", py_project, git_env, *args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Re-release:" in result.stdout
    assert "Re-release of v1.0.1 committed and pushed" in result.stdout
    head = git(py_project, git_env, "rev-parse", "HEAD")
    assert head != tagged
    assert git(py_project, git_env, "log", "-1", "--format=%s") == "Tweak"
    assert git(origin, git_env, "rev-parse", "main") == head
    assert git(py_project, git_env, "rev-parse", "v1.0.1^{commit}") == tagged
    assert remote_tag_commit(origin, git_env, "v1.0.1") == tagged


def test_release_force_pushes_unpushed_existing_tag(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """A tag that only exists locally is pushed as is, not re-created."""
    tagged = mark_released(py_project, git_env, push_tag=False)
    _write(py_project / "tweak.txt", "minor\n")

    result = run(
        scripts_dir / "release.sh", py_project, git_env, "-f", "1.0.1", "Tweak"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert remote_tag_commit(origin, git_env, "v1.0.1") == tagged
    assert git(origin, git_env, "rev-parse", "main") == git(
        py_project, git_env, "rev-parse", "HEAD"
    )


def test_release_force_with_new_version_is_a_normal_release(
    scripts_dir: Path,
    py_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """``--force`` has no effect when the version isn't tagged yet."""
    result = run(
        scripts_dir / "release.sh", py_project, git_env, "--force", "1.0.1", "New"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Re-release" not in result.stdout
    head = git(py_project, git_env, "rev-parse", "HEAD")
    assert remote_tag_commit(origin, git_env, "v1.0.1") == head


def test_release_usage_error_counts_positional_args(
    scripts_dir: Path,
    py_project: Path,
    git_env: dict[str, str],
) -> None:
    """``--force`` doesn't count towards the two required arguments."""
    result = run(scripts_dir / "release.sh", py_project, git_env, "--force", "1.0.1")

    assert result.returncode == 1
    assert "Usage:" in result.stdout
    assert "--force" in result.stdout


# --------------------------------------------------------------------------
# nopy_release.sh
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version_arg", ["1.0.1", "v1.0.1"])
def test_nopy_release_tags_and_pushes(
    scripts_dir: Path,
    nopy_project: Path,
    origin: Path,
    git_env: dict[str, str],
    version_arg: str,
) -> None:
    """A non-Python release is committed, tagged ``v1.0.1`` and pushed."""
    _write(nopy_project / "feature.txt", "new\n")

    result = run(
        scripts_dir / "nopy_release.sh", nopy_project, git_env, version_arg, "Feature"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    head = git(nopy_project, git_env, "rev-parse", "HEAD")
    assert remote_tag_commit(origin, git_env, "v1.0.1") == head
    assert not remote_tag_commit(origin, git_env, "vv1.0.1")


def test_nopy_release_refuses_existing_tag(
    scripts_dir: Path,
    nopy_project: Path,
    git_env: dict[str, str],
) -> None:
    """Re-releasing an existing version is refused before committing."""
    _write(nopy_project / "feature.txt", "new\n")
    head = git(nopy_project, git_env, "rev-parse", "HEAD")

    result = run(
        scripts_dir / "nopy_release.sh", nopy_project, git_env, "1.0.0", "Again"
    )

    assert result.returncode == 1
    assert "tag v1.0.0 already exists locally" in result.stdout
    assert git(nopy_project, git_env, "rev-parse", "HEAD") == head


def test_nopy_release_reports_failed_tag_push(
    scripts_dir: Path,
    nopy_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """A rejected tag push makes the script fail instead of reporting success."""
    reject_tag_pushes(origin)

    result = run(
        scripts_dir / "nopy_release.sh", nopy_project, git_env, "1.0.1", "Release"
    )

    assert result.returncode == 1
    assert "Error: git push origin v1.0.1 failed" in result.stdout
    assert "committed and pushed" not in result.stdout


def test_nopy_release_outside_git_repo(
    scripts_dir: Path,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    """Running outside a git repo fails with a clear error."""
    plain = tmp_path / "plain"
    plain.mkdir()
    env = {**git_env, "GIT_CEILING_DIRECTORIES": str(tmp_path)}

    result = run(scripts_dir / "nopy_release.sh", plain, env, "1.0.1", "Release")

    assert result.returncode == 1
    assert "Not a git repository" in result.stdout


def test_nopy_release_force_rereleases_without_moving_tag(
    scripts_dir: Path,
    nopy_project: Path,
    origin: Path,
    git_env: dict[str, str],
) -> None:
    """``--force`` re-releases an existing tag without moving it."""
    tagged = git(nopy_project, git_env, "rev-parse", "HEAD")
    _write(nopy_project / "tweak.txt", "minor\n")

    result = run(
        scripts_dir / "nopy_release.sh",
        nopy_project,
        git_env,
        "--force",
        "1.0.0",
        "Tweak",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Re-release of v1.0.0 committed and pushed" in result.stdout
    head = git(nopy_project, git_env, "rev-parse", "HEAD")
    assert head != tagged
    assert git(origin, git_env, "rev-parse", "main") == head
    assert remote_tag_commit(origin, git_env, "v1.0.0") == tagged
