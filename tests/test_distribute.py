"""Integration tests for ``distribute.sh``.

The distributor is a bash script, so these tests drive it as a subprocess
against throwaway fixture trees. Each test builds an isolated "pyutil" dir
(holding a copy of the real ``distribute.sh`` plus fake master scripts) and a
separate dev root containing fake client projects, then asserts on the files
written and the process exit status.
"""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTRIBUTE_SRC = REPO_ROOT / "distribute.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _read(path: Path) -> str:
    """Return the UTF-8 text contents of ``path``.

    Args:
        path: File to read.

    Returns:
        The file's text.
    """
    return path.read_text(encoding="utf-8")


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
def pyutil_dir(tmp_path: Path) -> Path:
    """Create a fake pyutil dir with the real script and master files.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Path to the fake pyutil directory.
    """
    root = tmp_path / "pyutil"
    root.mkdir()
    shutil.copy(DISTRIBUTE_SRC, root / "distribute.sh")
    _write(root / "launch.sh", '#!/bin/bash\necho "launch v1"\n')
    # A second master that is intentionally NOT in the rollout allowlist.
    _write(root / "servicectrl.sh", '#!/bin/bash\necho "svc v1"\n')
    return root


@pytest.fixture
def dev_root(tmp_path: Path) -> Path:
    """Create an empty dev root for fake client projects.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Path to the dev root directory.
    """
    root = tmp_path / "dev"
    root.mkdir()
    return root


def make_project(
    dev_root: Path,
    name: str,
    managed_scripts: list[str] | None,
) -> Path:
    """Create a fake client project, optionally with a ``[tool.pyutil]`` marker.

    Args:
        dev_root: Directory the project is created under.
        name: Project directory name.
        managed_scripts: Scripts to list in the marker, or ``None`` to omit the
            ``[tool.pyutil]`` section entirely.

    Returns:
        Path to the created project directory.
    """
    project = dev_root / name
    project.mkdir(parents=True, exist_ok=True)
    toml = f'[project]\nname = "{name}"\n'
    if managed_scripts is not None:
        rendered = ", ".join(f'"{s}"' for s in managed_scripts)
        toml += f"\n[tool.pyutil]\nmanaged_scripts = [{rendered}]\n"
    _write(project / "pyproject.toml", toml)
    return project


def run(pyutil_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``distribute.sh`` with ``args`` and capture the result.

    Args:
        pyutil_dir: Fake pyutil directory holding the script.
        *args: Command-line arguments passed to the script.

    Returns:
        The completed process, with stdout/stderr captured as text.
    """
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [BASH, str(pyutil_dir / "distribute.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_new_copy_is_written_and_stamped(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """A fresh opted-in project receives a stamped, executable copy."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    result = run(pyutil_dir, "--root", str(dev_root))

    copy = project / "scripts" / "launch.sh"
    assert result.returncode == 0
    assert copy.is_file()
    assert copy.stat().st_mode & 0o111  # executable
    body = _read(copy)
    assert "pyutil-managed" in body
    assert "pyutil-source-sha256:" in body
    assert 'echo "launch v1"' in body


def test_allowlist_filters_requested_scripts(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """A script requested but absent from MANAGED_SCRIPTS is not distributed."""
    project = make_project(dev_root, "projA", ["launch.sh", "servicectrl.sh"])
    run(pyutil_dir, "--root", str(dev_root))

    assert (project / "scripts" / "launch.sh").is_file()
    assert not (project / "scripts" / "servicectrl.sh").exists()


def test_project_without_marker_is_skipped(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """A project lacking [tool.pyutil] gets no scripts directory."""
    project = make_project(dev_root, "projB", None)
    result = run(pyutil_dir, "--root", str(dev_root))

    assert result.returncode == 0
    assert not (project / "scripts").exists()


def test_marker_only_non_allowlisted_copies_nothing(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """Opting into only a non-allowlisted script results in no copies."""
    project = make_project(dev_root, "projC", ["servicectrl.sh"])
    run(pyutil_dir, "--root", str(dev_root))

    assert not (project / "scripts").exists()


def test_idempotent_second_run_writes_nothing(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """Re-running with no master change reports up-to-date and writes nothing."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    run(pyutil_dir, "--root", str(dev_root))
    copy = project / "scripts" / "launch.sh"
    before = copy.read_bytes()

    result = run(pyutil_dir, "--root", str(dev_root))

    assert result.returncode == 0
    assert "up to date" in result.stdout
    assert copy.read_bytes() == before


def test_stale_copy_is_updated(pyutil_dir: Path, dev_root: Path) -> None:
    """Bumping the master refreshes an untouched copy to the new content."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    run(pyutil_dir, "--root", str(dev_root))

    _write(pyutil_dir / "launch.sh", '#!/bin/bash\necho "launch v2"\n')
    result = run(pyutil_dir, "--root", str(dev_root))

    assert result.returncode == 0
    assert 'echo "launch v2"' in _read(project / "scripts" / "launch.sh")


def test_local_edit_is_refused_without_force(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """A hand-edited copy is refused (unchanged, non-zero exit) without --force."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    run(pyutil_dir, "--root", str(dev_root))
    copy = project / "scripts" / "launch.sh"
    _write(copy, _read(copy) + '\necho "HAND EDIT"\n')
    edited = _read(copy)

    result = run(pyutil_dir, "--root", str(dev_root))

    assert result.returncode == 1
    combined = (result.stdout + result.stderr).lower()
    assert "refused" in combined
    assert _read(copy) == edited  # untouched


def test_force_overwrites_local_edit(pyutil_dir: Path, dev_root: Path) -> None:
    """--force overwrites a hand-edited copy back to the master content."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    run(pyutil_dir, "--root", str(dev_root))
    copy = project / "scripts" / "launch.sh"
    _write(copy, _read(copy) + '\necho "HAND EDIT"\n')

    result = run(pyutil_dir, "--root", str(dev_root), "--force")

    assert result.returncode == 0
    assert "HAND EDIT" not in _read(copy)


def test_check_reports_drift_and_writes_nothing(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """--check exits non-zero on drift and leaves files untouched."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    run(pyutil_dir, "--root", str(dev_root))
    copy = project / "scripts" / "launch.sh"
    _write(copy, _read(copy) + '\necho "HAND EDIT"\n')
    edited = _read(copy)

    result = run(pyutil_dir, "--check", "--root", str(dev_root))

    assert result.returncode == 1
    assert _read(copy) == edited


def test_check_clean_when_in_sync(pyutil_dir: Path, dev_root: Path) -> None:
    """--check exits zero when every copy is up to date."""
    make_project(dev_root, "projA", ["launch.sh"])
    run(pyutil_dir, "--root", str(dev_root))

    result = run(pyutil_dir, "--check", "--root", str(dev_root))

    assert result.returncode == 0


def test_unstamped_identical_copy_is_adopted(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """An unstamped copy identical to the master is adopted (stamped) safely."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    copy = project / "scripts" / "launch.sh"
    _write(copy, _read(pyutil_dir / "launch.sh"))

    result = run(pyutil_dir, "--root", str(dev_root))

    assert result.returncode == 0
    assert "pyutil-managed" in _read(copy)


def test_unstamped_divergent_copy_is_refused(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """An unstamped copy that differs from the master is refused without --force."""
    project = make_project(dev_root, "projA", ["launch.sh"])
    copy = project / "scripts" / "launch.sh"
    _write(copy, '#!/bin/bash\necho "foreign content"\n')

    result = run(pyutil_dir, "--root", str(dev_root))

    assert result.returncode == 1
    assert "foreign content" in _read(copy)  # untouched


def test_project_flag_scopes_to_one_project(
    pyutil_dir: Path,
    dev_root: Path,
) -> None:
    """--project restricts distribution to a single project directory."""
    proj_a = make_project(dev_root, "projA", ["launch.sh"])
    proj_b = make_project(dev_root, "projB", ["launch.sh"])

    result = run(pyutil_dir, "--project", str(proj_a))

    assert result.returncode == 0
    assert (proj_a / "scripts" / "launch.sh").is_file()
    assert not (proj_b / "scripts").exists()
