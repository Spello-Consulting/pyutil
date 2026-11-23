"""Tests for ``deploy_check.py``.

Most tests drive the pure parsing and decision helpers against fixture files
in ``tests/fixtures/deploy_check`` (workflows modelled on real projects, and
sudoers files in the form the Linux host guide uses). The project checks are
exercised end to end against a throwaway project in a fake home folder, with
network checks turned off.
"""

from __future__ import annotations

import os
import plistlib
import pwd
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from typing import Any

import pytest
import yaml

import deploy_check as dc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "deploy_check"
REPO_ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git") or "/usr/bin/git"


def load_fixture(name: str) -> Any:
    """Load a YAML fixture file.

    Args:
        name: File name in the fixtures folder.

    Returns:
        The parsed YAML.
    """
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def guide_rules() -> list[dc.SudoRule]:
    """Parse the sudoers file from the Linux host guide.

    Returns:
        Its rules for github-runner.
    """
    rules, unsupported = dc.parse_sudoers((FIXTURES / "sudoers_guide").read_text(encoding="utf-8"))
    assert unsupported == []
    return rules


def allowed(rules: list[dc.SudoRule], call: dc.SudoCall) -> bool:
    """Check whether any rule allows a call.

    Args:
        rules: Sudoers rules.
        call: The call.

    Returns:
        True if a rule allows it.
    """
    return any(dc.rule_allows(rule, call) for rule in rules)


# --------------------------------------------------------------------------
# sudo command lines
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "sudo -n -u nick /usr/bin/git -C /home/nick/scripts/pyutil fetch origin",
            dc.SudoCall("nick", None, "/usr/bin/git", ("-C", "/home/nick/scripts/pyutil", "fetch", "origin")),
        ),
        (
            "sudo --chdir=/home/nick/scripts/Demo /home/nick/scripts/pyutil/servicectrl.sh stop",
            dc.SudoCall("root", "/home/nick/scripts/Demo", "/home/nick/scripts/pyutil/servicectrl.sh", ("stop",)),
        ),
        (
            "sudo -D /srv/app -u pi -- /bin/true --flag",
            dc.SudoCall("pi", "/srv/app", "/bin/true", ("--flag",)),
        ),
        ("sudo --user=pi /bin/ls", dc.SudoCall("pi", None, "/bin/ls", ())),
    ],
)
def test_parse_sudo_call(line: str, expected: dc.SudoCall) -> None:
    """Options to sudo are understood, and the command and its arguments split off."""
    assert dc.parse_sudo_call(line.split()) == expected


@pytest.mark.parametrize("line", ["sudo -n", "sudo -u", "git status", ""])
def test_parse_sudo_call_ignores_non_calls(line: str) -> None:
    """Lines that are not sudo running a command give None."""
    assert dc.parse_sudo_call(line.split()) is None


def test_split_commands_handles_operators_comments_and_continuations() -> None:
    """Commands joined with && or ; are split, comments dropped, continuations joined."""
    script = "# set up\nsleep 6; sudo /bin/true\ncd ~/x && ./run.sh \\\n  --yes\necho 'a && b'\n"
    assert dc.split_commands(script) == [
        ["sleep", "6"],
        ["sudo", "/bin/true"],
        ["cd", "~/x"],
        ["./run.sh", "--yes"],
        ["echo", "a && b"],
    ]


def test_sudo_call_str_round_trips() -> None:
    """A call prints as the sudo command line that makes it."""
    call = dc.SudoCall("nick", "/a b", "/bin/x", ("y",))
    assert str(call) == "sudo -u nick '--chdir=/a b' /bin/x y"


# --------------------------------------------------------------------------
# sudoers parsing and matching
# --------------------------------------------------------------------------
def test_parse_guide_sudoers() -> None:
    """The four lines from the guide give four rules with the right options."""
    rules = guide_rules()
    assert [(r.runas, r.cwd, r.nopasswd, Path(r.command).name, r.args) for r in rules] == [
        (("ALL",), "*", True, "servicectrl.sh", "*"),
        (("ALL",), "*", True, "gitrefresh.sh", "*"),
        (("nick",), None, True, "git", "-C /home/nick/scripts/pyutil fetch origin"),
        (("nick",), None, True, "git", "-C /home/nick/scripts/pyutil reset --hard origin/main"),
    ]


def test_parse_mixed_sudoers() -> None:
    """Continuations, comma lists and "" are read; aliases are reported; other users skipped."""
    rules, unsupported = dc.parse_sudoers((FIXTURES / "sudoers_mixed").read_text(encoding="utf-8"))
    assert unsupported == ["Cmnd_Alias DEPLOY = /usr/bin/systemctl restart demo"]
    assert [(Path(r.command).name, r.args, r.nopasswd, r.cwd) for r in rules] == [
        ("servicectrl.sh", "*", True, None),
        ("git", "-C /home/nick/scripts/Old pull", True, None),
        ("git", "-C /home/nick/scripts/Demo pull", True, None),
        ("reboot", None, False, None),
        ("uptime", "", True, None),
    ]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (dc.SudoCall("root", "/home/nick/scripts/Demo", "/home/nick/scripts/pyutil/servicectrl.sh", ("stop",)), True),
        (dc.SudoCall("root", "/home/nick/scripts/Demo", "/home/nick/scripts/pyutil/gitrefresh.sh", ("--yes", "--branch", "main")), True),
        (dc.SudoCall("nick", None, "/usr/bin/git", ("-C", "/home/nick/scripts/pyutil", "fetch", "origin")), True),
        (dc.SudoCall("nick", None, "/usr/bin/git", ("-C", "/home/nick/scripts/pyutil", "reset", "--hard", "origin/main")), True),
        # Resetting to a branch other than main is not allowed by the guide's line.
        (dc.SudoCall("nick", None, "/usr/bin/git", ("-C", "/home/nick/scripts/pyutil", "reset", "--hard", "origin/feature")), False),
        # The git lines only allow running as nick, not root.
        (dc.SudoCall("root", None, "/usr/bin/git", ("-C", "/home/nick/scripts/pyutil", "fetch", "origin")), False),
        # Another user's pyutil (e.g. a pi-spello path on a nick host).
        (dc.SudoCall("root", "/home/pi/scripts/Demo", "/home/pi/scripts/pyutil/servicectrl.sh", ("stop",)), False),
    ],
)
def test_guide_rules_allow_what_deploys_run(call: dc.SudoCall, expected: bool) -> None:
    """The guide's sudoers lines allow exactly the deploy calls they are meant for."""
    assert allowed(guide_rules(), call) is expected


def test_chdir_needs_cwd_option() -> None:
    """--chdir is refused unless the rule has CWD=* (or the same folder)."""
    rules, _ = dc.parse_sudoers((FIXTURES / "sudoers_mixed").read_text(encoding="utf-8"))
    script = "/home/nick/scripts/pyutil/servicectrl.sh"
    assert allowed(rules, dc.SudoCall("root", None, script, ("stop",)))
    assert not allowed(rules, dc.SudoCall("root", "/home/nick/scripts/Demo", script, ("stop",)))
    exact, _ = dc.parse_sudoers(f"github-runner ALL=(ALL) CWD=/srv/a NOPASSWD: {script}\n")
    assert allowed(exact, dc.SudoCall("root", "/srv/a", script, ()))
    assert not allowed(exact, dc.SudoCall("root", "/srv/b", script, ()))


def test_password_and_empty_args_rules() -> None:
    """Rules that need a password never match; "" allows no arguments at all."""
    rules, _ = dc.parse_sudoers((FIXTURES / "sudoers_mixed").read_text(encoding="utf-8"))
    assert not allowed(rules, dc.SudoCall("root", None, "/usr/bin/reboot", ()))
    assert allowed(rules, dc.SudoCall("root", None, "/usr/bin/uptime", ()))
    assert not allowed(rules, dc.SudoCall("root", None, "/usr/bin/uptime", ("-p",)))


def test_wildcard_matches_across_spaces_and_all_command() -> None:
    """Like sudo, * in arguments matches across words, and ALL allows any command."""
    rules, _ = dc.parse_sudoers("github-runner ALL=(ALL:ALL) NOPASSWD: /bin/x -a *\ngithub-runner ALL=(pi) NOPASSWD: ALL\n")
    assert allowed(rules, dc.SudoCall("root", None, "/bin/x", ("-a", "b", "c")))
    assert not allowed(rules, dc.SudoCall("root", None, "/bin/x", ("-b",)))
    assert allowed(rules, dc.SudoCall("pi", None, "/anything", ("at", "all")))


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            dc.SudoCall("root", "/home/pi/scripts/Demo", "/home/pi/scripts/pyutil/gitrefresh.sh", ("--yes",)),
            "github-runner ALL=(ALL) CWD=* NOPASSWD: /home/pi/scripts/pyutil/gitrefresh.sh *",
        ),
        (
            dc.SudoCall("pi", None, "/usr/bin/git", ("-C", "/home/pi/scripts/pyutil", "fetch", "origin")),
            "github-runner ALL=(pi) NOPASSWD: /usr/bin/git -C /home/pi/scripts/pyutil fetch origin",
        ),
        (
            dc.SudoCall("root", None, "/bin/echo", ("a=b",)),
            "github-runner ALL=(ALL) NOPASSWD: /bin/echo a\\=b",
        ),
    ],
)
def test_suggest_rule(call: dc.SudoCall, expected: str) -> None:
    """Suggested lines use the guide's form and allow the call they were made for."""
    line = dc.suggest_rule(call)
    assert line == expected
    rules, _ = dc.parse_sudoers(line)
    assert allowed(rules, call)


# --------------------------------------------------------------------------
# deploy.yml
# --------------------------------------------------------------------------
def test_environment_options_reads_on_parsed_as_true() -> None:
    """PyYAML reads `on:` as True; the options are still found."""
    workflow = load_fixture("linux_deploy.yml")
    assert True in workflow
    assert dc.environment_options(workflow) == ["sydneyapp", "pi-spello", "all"]
    assert dc.environment_options({"on": "push"}) is None


def test_jobs_for_host_by_label() -> None:
    """A job matches by exact runs-on label; hosted jobs and case near-misses do not."""
    workflow = load_fixture("linux_deploy.yml")
    assert [j.job_id for j in dc.jobs_for_host(workflow, "sydneyapp")] == ["deploy-sydneyapp"]
    assert dc.jobs_for_host(workflow, "pi-spello") == []
    assert dc.runs_on_case_mismatch(workflow, "pi-spello") == ["Pi-Spello"]
    assert dc.jobs_for_host(workflow, "ubuntu-latest") == []


def test_jobs_for_host_by_computed_matrix() -> None:
    """A fromJSON matrix job matches any host that is an environment option."""
    workflow = load_fixture("macos_matrix_deploy.yml")
    for host in ("MacMiniAU", "MacMiniIT"):
        jobs = dc.jobs_for_host(workflow, host)
        assert [(j.job_id, j.via) for j in jobs] == [("deploy", "matrix")]
        assert dc.job_environment(jobs[0].job, host) == host
    assert dc.jobs_for_host(workflow, "sydneyapp") == []


def test_jobs_for_host_by_literal_matrix() -> None:
    """A literal matrix list matches only the hosts it lists."""
    workflow = load_fixture("literal_matrix_deploy.yml")
    jobs = dc.jobs_for_host(workflow, "sydneyapp")
    assert [j.job_id for j in jobs] == ["deploy"]
    assert dc.job_environment(jobs[0].job, "sydneyapp") == "sydneyapp"
    assert dc.jobs_for_host(workflow, "spelloapp") == []


def test_job_sudo_calls_substitutes_branch() -> None:
    """$BRANCH from ${{ github.ref_name }} becomes main, and every sudo call is found."""
    workflow = load_fixture("linux_deploy.yml")
    job = dc.jobs_for_host(workflow, "sydneyapp")[0].job
    calls = dc.job_sudo_calls(workflow, job, "sydneyapp")
    assert [(Path(c.command).name, c.args) for c in calls] == [
        ("servicectrl.sh", ("stop",)),
        ("gitrefresh.sh", ("--yes", "--branch", "main")),
        ("servicectrl.sh", ("deploy", "sydneyapp")),
        ("servicectrl.sh", ("is-active",)),
    ]
    assert {c.chdir for c in calls} == {"/home/nick/scripts/Demo"}
    assert all(allowed(guide_rules(), c) for c in calls)


def test_step_if_limits_steps_to_a_host() -> None:
    """A step with `if: matrix.environment == 'MacMiniAU'` only counts on that host."""
    workflow = load_fixture("macos_matrix_deploy.yml")
    job = workflow["jobs"]["deploy"]
    assert dc.job_uses_script(workflow, job, "MacMiniAU", "useragentctrl.sh")
    assert not dc.job_uses_script(workflow, job, "MacMiniIT", "useragentctrl.sh")
    assert dc.job_uses_script(workflow, job, "MacMiniIT", "gitrefresh.sh")


def test_pyutil_workflow_calls_match_guide_sudoers() -> None:
    """The pyutil deploy (plain git as nick) is allowed by the guide's lines."""
    workflow = load_fixture("pyutil_deploy.yml")
    job = dc.jobs_for_host(workflow, "sydneyapp")[0].job
    calls = dc.job_sudo_calls(workflow, job, "sydneyapp")
    assert len(calls) == 2
    assert all(allowed(guide_rules(), c) for c in calls)
    mac_job = dc.jobs_for_host(workflow, "MacMiniAU")[0].job
    assert dc.job_sudo_calls(workflow, mac_job, "MacMiniAU") == []


def test_substitute_leaves_unknown_expressions() -> None:
    """Only known expressions and given variables are filled in."""
    text = "${{ matrix.environment }} ${{ secrets.X }} $BRANCH ${BRANCH} $BRANCHES"
    assert dc.substitute(text, "h", {"BRANCH": "${{ github.ref_name }}"}) == "h ${{ secrets.X }} main main $BRANCHES"


# --------------------------------------------------------------------------
# auto-deploy.yaml
# --------------------------------------------------------------------------
OPTIONS = ["sydneyapp", "spelloapp", "pi-spello", "all"]


def test_auto_deploy_interval_fixture_is_valid() -> None:
    """The guide's example is valid and summarised."""
    review = dc.review_auto_deploy(load_fixture("auto_deploy_interval.yaml"), OPTIONS, "sydneyapp")
    assert review.errors == []
    assert review.warnings == []
    assert review.summary == "enabled; environments sydneyapp, spelloapp; every 6h; time zone Australia/Sydney"


def test_auto_deploy_at_fixture_is_valid() -> None:
    """Quoted At times and Environments: all are valid; TimeZone defaults."""
    review = dc.review_auto_deploy(load_fixture("auto_deploy_at.yaml"), OPTIONS, "pi-spello")
    assert review.errors == []
    assert review.summary == "enabled; all hosts; at 02:00, 14:30; time zone Australia/Sydney"


def test_auto_deploy_unquoted_time_is_an_error() -> None:
    """Unquoted 14:30 is read by YAML 1.1 as the number 870, which auto-deploy rejects."""
    review = dc.review_auto_deploy(load_fixture("auto_deploy_unquoted.yaml"), OPTIONS, "sydneyapp")
    assert len(review.errors) == 1
    assert "read as the number 870" in review.errors[0]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("just text", "must be a mapping"),
        ({"Environments": "all", "Schedule": {"Interval": "1h"}}, "Enabled is required"),
        ({"Enabled": "yes", "Environments": "all", "Schedule": {"Interval": "1h"}}, "Enabled must be true or false"),
        ({"Enabled": True, "Schedule": {"Interval": "1h"}}, "Environments is required"),
        ({"Enabled": True, "Environments": [], "Schedule": {"Interval": "1h"}}, "Environments must be all"),
        ({"Enabled": True, "Environments": "All", "Schedule": {"Interval": "1h"}}, "Environments must be all"),
        ({"Enabled": True, "Environments": ["nowhere", "x"], "Schedule": {"Interval": "1h"}}, "Environments nowhere, x not an option in deploy.yml"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"Interval": "1h"}, "Extra": 1}, "unknown setting 'Extra'"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"Interval": "1h", "Every": 2}}, "unknown Schedule setting 'Every'"),
        ({"Enabled": True, "Environments": "all"}, "Schedule is required"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"Interval": "1h", "At": ["02:00"]}}, "exactly one of Interval or At"),
        ({"Enabled": True, "Environments": "all", "Schedule": {}}, "exactly one of Interval or At"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"Interval": "5h"}}, "Schedule.Interval must be one of"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"At": "02:00"}}, "must be a list of quoted times"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"At": ["25:00"]}}, "not a 24-hour HH:MM time"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"Interval": "1h"}, "TimeZone": "Mars/Base"}, "not a known time zone"),
    ],
)
def test_auto_deploy_errors(data: object, message: str) -> None:
    """Each rule from the auto-deploy guide is enforced."""
    review = dc.review_auto_deploy(data, OPTIONS, "sydneyapp")
    assert any(message in e for e in review.errors), review.errors
    assert not review.summary


@pytest.mark.parametrize(
    ("data", "host", "message"),
    [
        ({"Enabled": False, "Environments": "all", "Schedule": {"Interval": "1h"}}, "sydneyapp", "Enabled is false"),
        ({"Enabled": True, "Environments": ["spelloapp"], "Schedule": {"Interval": "1h"}}, "sydneyapp", "does not include this host"),
        ({"Enabled": True, "Environments": "all", "Schedule": {"Interval": "1h"}}, "MacMiniAU", "'all' does not include it"),
    ],
)
def test_auto_deploy_warnings(data: object, host: str, message: str) -> None:
    """Valid but probably unintended settings give warnings, not errors."""
    review = dc.review_auto_deploy(data, OPTIONS, host)
    assert review.errors == []
    assert any(message in w for w in review.warnings), review.warnings


def test_auto_deploy_names_are_case_insensitive() -> None:
    """Environments and Interval match without regard to case, as auto-deploy does."""
    data = {"Enabled": True, "Environments": ["SydneyApp"], "Schedule": {"Interval": "6H"}}
    review = dc.review_auto_deploy(data, OPTIONS, "sydneyapp")
    assert review.errors == []
    assert review.warnings == []
    assert "every 6h" in review.summary


def test_auto_deploy_pseudo_option_includes_host() -> None:
    """A list naming the 'all' option (as pyutil's file does) covers every host."""
    data = {"Enabled": True, "Environments": ["all"], "Schedule": {"Interval": "1h"}, "TimeZone": None}
    review = dc.review_auto_deploy(data, OPTIONS, "pi-spello")
    assert review.errors == []
    assert review.warnings == []


# --------------------------------------------------------------------------
# Ownership, remotes, env files, pyproject
# --------------------------------------------------------------------------
def test_find_foreign_owned(tmp_path: Path) -> None:
    """Files owned by the expected user pass; any other expected owner lists them."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "FETCH_HEAD").write_text("x", encoding="utf-8")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert dc.find_foreign_owned(tmp_path, os.getuid()) == []
    found = dc.find_foreign_owned(tmp_path, os.getuid() + 1, limit=10)
    assert found[0].startswith(f"{tmp_path} (owner ")
    assert {Path(f.split(" (owner")[0]).name for f in found} == {tmp_path.name, ".git", "FETCH_HEAD", "a.txt"}
    assert len(dc.find_foreign_owned(tmp_path, os.getuid() + 1, limit=2)) == 2


def test_find_foreign_owned_does_not_follow_symlinks(tmp_path: Path) -> None:
    """A symlinked folder is checked itself, not scanned."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(3):
        (outside / f"f{i}").write_text("x", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link").symlink_to(outside)
    found = dc.find_foreign_owned(repo, os.getuid() + 1, limit=50)
    assert [Path(f.split(" (owner")[0]).name for f in found] == ["repo", "link"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Spello-Consulting/pyutil.git", ("Spello-Consulting", "pyutil")),
        ("https://github.com/Spello-Consulting/pyutil", ("Spello-Consulting", "pyutil")),
        ("https://token@github.com/Spello-Consulting/safe-share.git\n", ("Spello-Consulting", "safe-share")),
        ("git@github.com:NickElseySpelloC/YahooFinance.git", ("NickElseySpelloC", "YahooFinance")),
        ("ssh://git@github.com/Spello-Consulting/water-info.git", ("Spello-Consulting", "water-info")),
        ("https://gitlab.com/a/b.git", None),
        ("/srv/git/pyutil.git", None),
    ],
)
def test_parse_github_remote(url: str, expected: tuple[str, str] | None) -> None:
    """HTTPS and SSH GitHub remotes are split; anything else gives None."""
    assert dc.parse_github_remote(url) == expected


def test_parse_env_file_and_expand_home() -> None:
    """Assignments are read with quotes and comments handled; $HOME and ~ expand."""
    text = '# comment\nexport APP_ENV=production\nAPP_CONFIG="$HOME/scripts/x/configs/production.yaml"\nTOKEN=op://Dev/x/credential  # secret\nnot a line\n'
    values = dc.parse_env_file(text)
    assert values == {
        "APP_ENV": "production",
        "APP_CONFIG": "$HOME/scripts/x/configs/production.yaml",
        "TOKEN": "op://Dev/x/credential",
    }
    home = Path("/home/nick")
    assert dc.expand_home(values["APP_CONFIG"], home) == "/home/nick/scripts/x/configs/production.yaml"
    assert dc.expand_home("~/a", home) == "/home/nick/a"
    assert dc.expand_home("${HOME}/a", home) == "/home/nick/a"


@pytest.mark.parametrize(
    ("pyproject", "expected"),
    [
        ({"project": {"service_name": "safe-share"}}, ["safe-share"]),
        ({"project": {"service_name": ["safe-share", "cloudflared"]}}, ["safe-share", "cloudflared"]),
        ({"project": {"service_name": ""}}, []),
        ({"project": {}}, []),
        ({}, []),
    ],
)
def test_project_names(pyproject: dict[str, Any], expected: list[str]) -> None:
    """service_name may be a string or a list."""
    assert dc.project_names(pyproject, "service_name") == expected


def test_unit_paths() -> None:
    """The files a unit needs are found; optional EnvironmentFile=- is skipped."""
    unit = (
        "[Service]\nExecStart=/home/nick/scripts/Demo/scripts/launch.sh --x\nWorkingDirectory=/home/nick/scripts/Demo\n"
        "EnvironmentFile=/home/nick/.config/op/service-account-token\nEnvironmentFile=-/etc/default/demo\nUser=nick\n"
    )
    assert dc._unit_paths(unit) == [  # noqa: SLF001
        ("ExecStart", "/home/nick/scripts/Demo/scripts/launch.sh"),
        ("WorkingDirectory", "/home/nick/scripts/Demo"),
        ("EnvironmentFile", "/home/nick/.config/op/service-account-token"),
    ]


def test_service_working_directory(tmp_path: Path) -> None:
    """The runner folder is read from its LaunchAgent plist or systemd unit."""
    plist = tmp_path / "actions.runner.Spello-Consulting.MacMiniAU.plist"
    plist.write_bytes(plistlib.dumps({"Label": "x", "WorkingDirectory": "/Users/nick/scripts/github-runner"}))
    unit = tmp_path / "actions.runner.Spello-Consulting.sydneyapp.service"
    unit.write_text("[Service]\nExecStart=/home/github-runner/actions-runner/runsvc.sh\nWorkingDirectory=/home/github-runner/actions-runner\n", encoding="utf-8")
    assert dc.service_working_directory(plist) == Path("/Users/nick/scripts/github-runner")
    assert dc.service_working_directory(unit) == Path("/home/github-runner/actions-runner")
    assert dc.service_working_directory(tmp_path / "missing.plist") is None


# --------------------------------------------------------------------------
# Project checks, end to end
# --------------------------------------------------------------------------
LABEL = "com.example.demo"


def git(cwd: Path, *args: str) -> None:
    """Run git quietly, isolated from the user's config.

    Args:
        cwd: Folder to run in.
        *args: Git arguments.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(  # noqa: S603
        [GIT, "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create ~/scripts/Demo: a macOS agent project that passes every project check.

    Args:
        tmp_path: pytest-provided temporary directory (used as the home folder).

    Returns:
        The project folder.
    """
    root = tmp_path / "scripts" / "Demo"
    (root / ".github" / "workflows").mkdir(parents=True)
    shutil.copy(FIXTURES / "macos_matrix_deploy.yml", root / ".github" / "workflows" / "deploy.yml")
    (root / "pyproject.toml").write_text(f'[project]\nname = "demo"\nversion = "1.0.0"\nuser_agent_name = "{LABEL}"\n', encoding="utf-8")
    (root / "deploy").mkdir()
    (root / "deploy" / f"{LABEL}.plist").write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/bin/sh", "-c", "true"]}))
    (root / "deploy" / "auto-deploy.yaml").write_text("Enabled: true\nEnvironments: [MacMiniAU]\nSchedule:\n  Interval: 1h\n", encoding="utf-8")
    (root / "configs").mkdir()
    (root / "configs" / "production.yaml").write_text("x: 1\n", encoding="utf-8")
    (root / ".env.dev.template").write_text("APP_ENV=development\nAPP_CONFIG=configs/development.yaml\n", encoding="utf-8")
    (root / ".env.prod.template").write_text('APP_ENV=production\nAPP_CONFIG="$HOME/scripts/Demo/configs/production.yaml"\n', encoding="utf-8")
    (root / ".gitignore").write_text(".env.target\n", encoding="utf-8")
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    git(root, "remote", "add", "origin", "https://github.com/Spello-Consulting/Demo.git")
    (root / ".env.target").symlink_to(".env.prod.template")
    installed = tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    installed.parent.mkdir(parents=True)
    shutil.copy(root / "deploy" / f"{LABEL}.plist", installed)
    return root


def run_project_checks(project: Path, host: str = "MacMiniAU") -> dict[str, dc.Result]:
    """Run the project checks offline, as on a macOS host.

    Args:
        project: Project folder (in ``<home>/scripts``).
        host: Host to check for.

    Returns:
        Results keyed by title.
    """
    return run_project_checks_at(project, project.parent.parent, host)


def run_project_checks_at(project: Path, home: Path, host: str = "MacMiniAU") -> dict[str, dc.Result]:
    """Run the project checks offline, as on a macOS host, with a given home folder.

    Args:
        project: Project folder.
        home: Home folder to use.
        host: Host to check for.

    Returns:
        Results keyed by title.
    """
    ctx = dc.Context(
        system="macos",
        host=host,
        host_source="--host",
        user="nick",
        uid=os.getuid(),
        home=home,
        project=project,
        online=False,
        use_sudo=False,
        script_dir=home / "scripts" / "pyutil",
        runner_dir=home / "actions-runner",
        runner_dir_source="default location",
    )
    section = dc.Section("Project")
    dc.check_project(ctx, section, project)
    return {r.title: r for r in section.results}


def statuses(results: dict[str, dc.Result]) -> dict[str, dc.Status]:
    """Map each title to its status.

    Args:
        results: Results keyed by title.

    Returns:
        Status keyed by title.
    """
    return {title: r.status for title, r in results.items()}


def test_project_checks_pass_for_a_good_project(project: Path) -> None:
    """A correctly set-up project gives no failures or warnings."""
    results = run_project_checks(project)
    assert statuses(results) == {
        "Project is in ~/scripts/Demo": dc.Status.PASS,
        "origin is Spello-Consulting/Demo": dc.Status.PASS,
        "Demo files are owned by nick": dc.Status.PASS,
        "Demo is on main": dc.Status.PASS,
        "Demo has no local changes": dc.Status.PASS,
        ".env.target links to a production template": dc.Status.PASS,
        "Config file exists": dc.Status.PASS,
        "deploy.yml deploys to MacMiniAU": dc.Status.PASS,
        f"User agent plist for {LABEL}": dc.Status.PASS,
        f"User agent {LABEL} is installed": dc.Status.PASS,
        "deploy/auto-deploy.yaml is valid": dc.Status.PASS,
        "GitHub Environment MacMiniAU exists": dc.Status.SKIP,
    }


def test_project_checks_on_a_host_without_the_agent(project: Path) -> None:
    """On MacMiniIT the agent step does not run, and auto-deploy does not list the host."""
    results = statuses(run_project_checks(project, host="MacMiniIT"))
    assert results["User agent"] is dc.Status.SKIP
    assert results["deploy/auto-deploy.yaml is valid"] is dc.Status.WARN


def test_project_checks_find_setup_mistakes(project: Path) -> None:
    """Common mistakes each fail the check that covers them."""
    (project / ".env.target").unlink()
    (project / ".env.target").symlink_to(".env.dev.template")
    (project / "deploy" / f"{LABEL}.plist").unlink()
    (project / "deploy" / "auto-deploy.yaml").write_text("Enabled: true\nEnvironments: [MacMiniAU]\nSchedule:\n  At: [14:30]\n", encoding="utf-8")
    results = run_project_checks(project, host="MacMiniXX")
    status = statuses(results)
    assert status[".env.target links to a production template"] is dc.Status.FAIL
    assert status["deploy.yml deploys to MacMiniXX"] is dc.Status.FAIL
    assert "'MacMiniXX' is not an environment option" in results["deploy.yml deploys to MacMiniXX"].detail[0]
    assert status["Demo has no local changes"] is dc.Status.WARN
    assert status["deploy/auto-deploy.yaml is valid"] is dc.Status.FAIL

    status = statuses(run_project_checks(project))
    assert status[f"User agent plist for {LABEL}"] is dc.Status.FAIL


def test_project_checks_report_wrong_location_and_dev_checkout(project: Path, tmp_path: Path) -> None:
    """A clone outside ~/scripts, with dev markers, is reported rather than crashing."""
    moved = tmp_path / "dev" / "Demo"
    moved.parent.mkdir()
    project.rename(moved)
    (moved / ".ruff_cache").mkdir()
    results = statuses(run_project_checks_at(moved, tmp_path))
    assert results["Folder is a deployed clone"] is dc.Status.WARN
    assert results["Project is in ~/scripts/Demo"] is dc.Status.FAIL


# --------------------------------------------------------------------------
# Output and wrapper
# --------------------------------------------------------------------------
def test_render_counts_and_fix_lines() -> None:
    """The report shows details, fixes and a summary line."""
    section = dc.Section("Host")
    section.ok("a")
    section.fail("b", "broken", ["step 1", "step 2"])
    section.skip("c", "n/a")
    text = dc.render([section, dc.Section("Empty")], color=False)
    assert text.splitlines() == [
        "Host",
        "  PASS  a",
        "  FAIL  b",
        "        broken",
        "        Fix: step 1",
        "             step 2",
        "  SKIP  c",
        "        n/a",
        "",
        "Summary: 1 passed, 0 warnings, 1 failed, 1 skipped",
    ]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_wrapper_runs_the_checker() -> None:
    """deploy_check.sh finds uv and runs deploy_check.py with its dependencies."""
    result = subprocess.run(  # noqa: S603
        [str(REPO_ROOT / "deploy_check.sh"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: deploy_check.sh" in result.stdout


# --------------------------------------------------------------------------
# Linux host checks, with system commands stubbed
# --------------------------------------------------------------------------
def linux_context(home: Path) -> dc.Context:
    """Build a Linux context for host sydneyapp with the given home folder.

    Args:
        home: Fake home folder.

    Returns:
        The context.
    """
    return dc.Context(
        system="linux",
        host="sydneyapp",
        host_source="runner",
        user="nick",
        uid=os.getuid(),
        home=home,
        project=None,
        online=False,
        use_sudo=True,
        script_dir=home / "scripts" / "pyutil",
        runner_dir=home / "runner",
        runner_dir_source="from /etc/systemd/system/actions.runner.Spello-Consulting.sydneyapp.service",
    )


@pytest.fixture
def linux_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dc.Context, Path]:
    """Set up a fake, correctly configured Linux host.

    The guide's sudoers lines and pyutil's workflow are rewritten to use the
    fake home folder; systemctl, sudo and git's system config are stubbed.

    Args:
        tmp_path: pytest-provided temporary directory (the home folder).
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        The context and the sudoers file it reads.
    """
    home = tmp_path
    pyutil = home / "scripts" / "pyutil"
    (pyutil / ".github" / "workflows").mkdir(parents=True)
    workflow = (FIXTURES / "pyutil_deploy.yml").read_text(encoding="utf-8").replace("/home/nick", str(home))
    (pyutil / ".github" / "workflows" / "deploy.yml").write_text(workflow, encoding="utf-8")
    sudoers = home / "sudoers"
    sudoers.write_text((FIXTURES / "sudoers_guide").read_text(encoding="utf-8").replace("/home/nick", str(home)), encoding="utf-8")
    (home / "runner").mkdir()
    (home / "runner" / ".service").write_text("actions.runner.Spello-Consulting.sydneyapp.service\n", encoding="utf-8")
    user_uv = home / ".local" / "bin" / "uv"
    user_uv.parent.mkdir(parents=True)
    user_uv.write_text("#!/bin/sh\n", encoding="utf-8")
    user_uv.chmod(0o755)
    root_uv = home / "usr-local-bin-uv"
    root_uv.symlink_to(user_uv)

    replies = {
        "sudo -n passwd -S github-runner": "github-runner L 2026-01-01 0 99999 7 -1\n",
        "systemctl is-active actions.runner.Spello-Consulting.sydneyapp.service": "active\n",
        "systemctl is-enabled actions.runner.Spello-Consulting.sydneyapp.service": "enabled\n",
        "systemctl show -p User --value actions.runner.Spello-Consulting.sydneyapp.service": "github-runner\n",
        "git config --system --get-all safe.directory": f"{pyutil}\n",
        f"sudo -n visudo -c -f {sudoers}": f"{sudoers}: parsed OK\n",
    }

    def fake_run(cmd: list[str], **_kwargs: object) -> dc.CmdResult:
        reply = replies.get(" ".join(cmd))
        return dc.CmdResult(0, reply, "") if reply is not None else dc.CmdResult(1, "", "not stubbed")

    def fake_getpwnam(name: str) -> object:
        if name == dc.RUNNER_USER:
            return object()
        raise KeyError(name)

    monkeypatch.setattr(dc, "run", fake_run)
    monkeypatch.setattr(pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(dc, "SUDOERS_FILE", sudoers)
    monkeypatch.setattr(dc, "ROOT_UV", root_uv)
    return linux_context(home), sudoers


def run_linux_checks(ctx: dc.Context) -> dict[str, dc.Result]:
    """Run the Linux host checks.

    Args:
        ctx: Linux context.

    Returns:
        Results keyed by title.
    """
    section = dc.Section("Linux")
    dc.check_host_linux(ctx, section)
    return {r.title: r for r in section.results}


def test_linux_host_checks_pass_for_a_good_host(linux_host: tuple[dc.Context, Path]) -> None:
    """A host set up as the Linux guide says passes every Linux check."""
    ctx, _ = linux_host
    results = run_linux_checks(ctx)
    assert {t: r.status for t, r in results.items()} == {
        "User github-runner exists": dc.Status.PASS,
        "github-runner's password is locked": dc.Status.PASS,
        "Runner service is running": dc.Status.PASS,
        "uv is installed for nick": dc.Status.PASS,
        "uv can be found by root": dc.Status.PASS,
        "pyutil is a git safe.directory": dc.Status.PASS,
        f"{dc.SUDOERS_FILE} is valid": dc.Status.PASS,
        "sudoers allows the pyutil deploy and the deploy scripts": dc.Status.PASS,
    }
    assert results["sudoers allows the pyutil deploy and the deploy scripts"].detail == ("4 sudo call(s) checked",)


def test_linux_sudoers_missing_lines_and_legacy_pull(linux_host: tuple[dc.Context, Path]) -> None:
    """A missing gitrefresh line fails with the line to add; an unused pull line warns."""
    ctx, sudoers = linux_host
    lines = [line for line in sudoers.read_text(encoding="utf-8").splitlines() if "gitrefresh.sh" not in line]
    lines.append(f"github-runner ALL=(ALL) NOPASSWD: /usr/bin/git -C {ctx.home}/scripts/Gone pull")
    sudoers.write_text("\n".join(lines) + "\n", encoding="utf-8")
    results = run_linux_checks(ctx)
    blocked = results["sudoers allows the pyutil deploy and the deploy scripts"]
    assert blocked.status is dc.Status.FAIL
    assert blocked.fix[1] == f"github-runner ALL=(ALL) CWD=* NOPASSWD: {ctx.pyutil_dir}/gitrefresh.sh *"
    assert results["No unused legacy 'git pull' sudoers lines"].status is dc.Status.WARN


def test_linux_checks_without_sudo_warn(linux_host: tuple[dc.Context, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """When root-only files cannot be read, the checks warn instead of failing."""
    ctx, _ = linux_host
    ctx.use_sudo = False
    monkeypatch.setattr(dc, "SUDOERS_FILE", Path("/nonexistent/dir/sudoers"))
    monkeypatch.setattr(dc, "read_text", lambda _path, *, use_sudo: (None, "needs sudo") if not use_sudo else (None, "missing"))
    results = run_linux_checks(ctx)
    assert results["github-runner's password is locked"].status is dc.Status.WARN
    assert results[f"{dc.SUDOERS_FILE} is valid"].status is dc.Status.WARN


def test_linux_workflow_for_another_users_paths_is_reported(linux_host: tuple[dc.Context, Path]) -> None:
    """A pyutil job that uses another user's paths (e.g. pi on a nick host) is reported."""
    ctx, _ = linux_host
    workflow = ctx.pyutil_dir / ".github" / "workflows" / "deploy.yml"
    workflow.write_text(workflow.read_text(encoding="utf-8").replace(f"-u nick /usr/bin/git -C {ctx.home}", "-u pi /usr/bin/git -C /home/pi"), encoding="utf-8")
    results = run_linux_checks(ctx)
    assert results["sudoers allows the pyutil deploy and the deploy scripts: paths and users match this host"].status is dc.Status.WARN
    assert results["sudoers allows the pyutil deploy and the deploy scripts"].status is dc.Status.FAIL
