#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Check that this host and the project in the current folder are ready for auto-deployment.

Runs read-only checks based on the deployment guides at
https://spello-consulting.github.io/automation/deployment/ (set up a Linux or
macOS host, add a project to a host, automatic deployments) and prints PASS,
WARN, FAIL or SKIP for each one, with the fix for anything that fails.

It never changes anything. Checks that need root on Linux use ``sudo -n`` and
report a warning when sudo is not available, rather than prompting.

Normally started through ``deploy_check.sh``, which runs this file with uv::

    cd ~/scripts/<Project>
    ~/scripts/pyutil/deploy_check.sh

Exit status is 0 when nothing failed (warnings allowed) and 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import getpass
import json
import os
import platform
import plistlib
import pwd
import re
import shlex
import shutil
import socket
import subprocess  # noqa: S404
import sys
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ORG = "Spello-Consulting"
ORG_URL = f"https://github.com/{ORG}"
DOCS_URL = "https://spello-consulting.github.io/automation/deployment/"
RUNNER_USER = "github-runner"
LINUX_RUNNER_DIR = Path("/home/github-runner/actions-runner")
SYSTEMD_DIR = Path("/etc/systemd/system")
SUDOERS_FILE = Path("/etc/sudoers.d/github-runner")
ROOT_UV = Path("/usr/local/bin/uv")
WORKFLOW = Path(".github/workflows/deploy.yml")
AUTO_DEPLOY = Path("deploy/auto-deploy.yaml")
AUTO_DEPLOY_INTERVALS = ("15m", "30m", "1h", "2h", "3h", "4h", "6h", "8h", "12h", "24h")
AUTO_DEPLOY_KEYS = frozenset({"Enabled", "Environments", "Schedule", "TimeZone"})
PSEUDO_ENVIRONMENTS = frozenset({"all", "both"})
DEFAULT_TIMEZONE = "Australia/Sydney"
DEV_MARKERS = (".dev_workspace", ".development", ".local_dev", ".devcontainer", ".ruff_cache")
DEV_PATH_PATTERNS = ("Development",)
OWNERSHIP_SAMPLE = 5
CMD_TIMEOUT = 20.0

GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
)
EXPRESSION_RE = re.compile(r"\$\{\{\s*(?P<expr>.*?)\s*\}\}")
MATRIX_LABEL_RE = re.compile(r"^\$\{\{\s*matrix\.(?P<key>[\w-]+)\s*\}\}$")
STEP_IF_RE = re.compile(
    r"^(?:\$\{\{)?\s*(?:matrix|inputs|github\.event\.inputs)\.environment\s*"
    r"(?P<op>==|!=)\s*'(?P<value>[^']*)'\s*(?:\}\})?$",
)
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SUDO_OPTION_RE = re.compile(r"^(?P<name>CWD|CHROOT|ROLE|TYPE|TIMEOUT|NOTBEFORE|NOTAFTER)=(?P<value>\S+)\s*")
SUDO_TAG_RE = re.compile(
    r"^(?P<tag>NOPASSWD|PASSWD|SETENV|NOSETENV|EXEC|NOEXEC|FOLLOW|NOFOLLOW|"
    r"LOG_INPUT|NOLOG_INPUT|LOG_OUTPUT|NOLOG_OUTPUT|MAIL|NOMAIL|INTERCEPT|NOINTERCEPT)\s*:\s*",
)
SUDO_ALIAS_KEYWORDS = ("User_Alias", "Runas_Alias", "Host_Alias", "Cmnd_Alias", "Cmd_Alias")
SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "&"})


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
Lines = str | list[str] | tuple[str, ...]


class Status(Enum):
    """Outcome of a single check."""

    PASS = "PASS"  # noqa: S105
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Result:
    """One reported check.

    Attributes:
        status: Outcome of the check.
        title: What was checked.
        detail: Extra lines explaining the outcome.
        fix: Commands or instructions that fix a failure or warning.
    """

    status: Status
    title: str
    detail: tuple[str, ...] = ()
    fix: tuple[str, ...] = ()


@dataclass
class Section:
    """A titled group of results.

    Attributes:
        title: Heading printed above the results.
        results: Results added so far.
    """

    title: str
    results: list[Result] = field(default_factory=list)

    def add(self, status: Status, title: str, detail: Lines = (), fix: Lines = ()) -> None:
        """Add a result.

        Args:
            status: Outcome of the check.
            title: What was checked.
            detail: One line, or several, explaining the outcome.
            fix: One line, or several, saying how to fix it.
        """
        self.results.append(Result(status, title, _lines(detail), _lines(fix)))

    def ok(self, title: str, detail: Lines = ()) -> None:
        """Add a PASS result.

        Args:
            title: What was checked.
            detail: Optional extra lines.
        """
        self.add(Status.PASS, title, detail)

    def warn(self, title: str, detail: Lines = (), fix: Lines = ()) -> None:
        """Add a WARN result.

        Args:
            title: What was checked.
            detail: Why it is a warning.
            fix: How to fix it.
        """
        self.add(Status.WARN, title, detail, fix)

    def fail(self, title: str, detail: Lines = (), fix: Lines = ()) -> None:
        """Add a FAIL result.

        Args:
            title: What was checked.
            detail: Why it failed.
            fix: How to fix it.
        """
        self.add(Status.FAIL, title, detail, fix)

    def skip(self, title: str, detail: Lines = ()) -> None:
        """Add a SKIP result (check not applicable or not possible here).

        Args:
            title: What was not checked.
            detail: Why it was skipped.
        """
        self.add(Status.SKIP, title, detail)


def _lines(value: Lines) -> tuple[str, ...]:
    """Normalise a string or a sequence of strings into a tuple of lines.

    Args:
        value: A single string, or a list or tuple of strings.

    Returns:
        The lines as a tuple; empty for an empty string or sequence.
    """
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(value)


# --------------------------------------------------------------------------
# Running commands and reading files
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CmdResult:
    """Outcome of an external command.

    Attributes:
        returncode: Exit status (127 if not found, 124 if it timed out).
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command exited with status 0."""
        return self.returncode == 0


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = CMD_TIMEOUT) -> CmdResult:
    """Run a command without a terminal, capturing its output.

    Args:
        cmd: Command and arguments.
        cwd: Directory to run it in.
        env: Extra environment variables, added to the current environment.
        timeout: Seconds to wait before giving up.

    Returns:
        The command's exit status and output. A missing command gives 127 and
        a timeout gives 124, instead of raising.
    """
    full_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=cwd,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return CmdResult(127, "", f"{cmd[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"{cmd[0]}: timed out after {timeout:.0f}s")
    except OSError as exc:
        return CmdResult(126, "", f"{cmd[0]}: {exc}")
    return CmdResult(proc.returncode, proc.stdout, proc.stderr)


GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10"),
}


def git(repo: Path, *args: str, timeout: float = CMD_TIMEOUT) -> CmdResult:
    """Run a read-only git command in ``repo`` that never prompts.

    Args:
        repo: Repository folder.
        *args: Git arguments.
        timeout: Seconds to wait.

    Returns:
        The command result.
    """
    return run(["git", "-C", str(repo), *args], env=GIT_ENV, timeout=timeout)


def read_text(path: Path, *, use_sudo: bool) -> tuple[str | None, str]:
    """Read a text file, falling back to ``sudo -n cat`` if permission is denied.

    Args:
        path: File to read.
        use_sudo: Whether ``sudo -n`` may be tried.

    Returns:
        ``(text, "")`` on success, else ``(None, reason)`` where reason is
        ``"missing"``, ``"needs sudo"`` or an error message. A leading UTF-8
        byte order mark is removed.
    """
    try:
        return path.read_text(encoding="utf-8-sig"), ""
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        if not use_sudo:
            return None, "needs sudo"
    except OSError as exc:
        return None, str(exc)
    result = run(["sudo", "-n", "cat", str(path)])
    if result.ok:
        return result.stdout.removeprefix("﻿"), ""
    if "No such file" in result.stderr:
        return None, "missing"
    return None, "needs sudo"


def which(name: str, *extra: Path) -> Path | None:
    """Find a command on PATH, or at one of the extra locations given.

    Args:
        name: Command name.
        *extra: Further full paths to try, in order.

    Returns:
        Path to the first executable found, or None.
    """
    found = shutil.which(name)
    if found:
        return Path(found)
    for candidate in extra:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def tilde(path: Path | str, home: Path) -> str:
    """Show a path with the home folder written as ``~``.

    Args:
        path: Path to show.
        home: Home folder to abbreviate.

    Returns:
        The path as text, starting with ``~`` if it is inside ``home``.
    """
    text = str(path)
    home_text = str(home)
    if text == home_text:
        return "~"
    if text.startswith(home_text + "/"):
        return "~" + text[len(home_text) :]
    return text


# --------------------------------------------------------------------------
# Pure helpers: remotes, ownership, env files
# --------------------------------------------------------------------------
def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Split a GitHub remote URL into owner and repository name.

    Args:
        url: HTTPS or SSH remote URL.

    Returns:
        ``(owner, repo)`` without any ``.git`` suffix, or None if the URL is
        not a GitHub repository URL.
    """
    match = GITHUB_REMOTE_RE.match(url.strip())
    if not match:
        return None
    return match["owner"], match["repo"]


def find_foreign_owned(root: Path, uid: int, limit: int = OWNERSHIP_SAMPLE) -> list[str]:
    """Find files and folders under ``root`` (and ``root`` itself) not owned by ``uid``.

    Symlinks are checked themselves, never followed. Folders that cannot be
    read are reported too, since they are usually owned by someone else.

    Args:
        root: Folder to scan.
        uid: Expected owner's user id.
        limit: Stop after finding this many.

    Returns:
        Up to ``limit`` offending paths, each with its owner, e.g.
        ``"/x/.git/FETCH_HEAD (owner root)"``.
    """
    found: list[str] = []

    def note(path: str, owner_uid: int | None) -> bool:
        if owner_uid is None:
            found.append(f"{path} (cannot read)")
        else:
            found.append(f"{path} (owner {_user_name(owner_uid)})")
        return len(found) >= limit

    try:
        root_uid = root.lstat().st_uid
        if root_uid != uid and note(str(root), root_uid):
            return found
    except OSError:
        note(str(root), None)
        return found

    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                if _scan_entries(entries, uid, note, stack):
                    return found
        except OSError:
            if note(current, None):
                return found
    return found


def _scan_entries(entries: Iterator[os.DirEntry[str]], uid: int, note: Callable[[str, int | None], bool], stack: list[str]) -> bool:
    """Check one folder's entries for ``find_foreign_owned``.

    Args:
        entries: The folder's entries.
        uid: Expected owner's user id.
        note: Records an offending path; returns True once enough are found.
        stack: Folders still to scan; subfolders are added to it.

    Returns:
        True when enough offending paths have been found.
    """
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            if note(entry.path, None):
                return True
            continue
        if info.st_uid != uid and note(entry.path, info.st_uid):
            return True
        if entry.is_dir(follow_symlinks=False):
            stack.append(entry.path)
    return False


def _user_name(uid: int) -> str:
    """Return the login name for ``uid``, or the number if it has none.

    Args:
        uid: User id.

    Returns:
        The user name, or the uid as text.
    """
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def parse_env_file(text: str) -> dict[str, str]:
    """Read ``KEY=value`` assignments from an env file or env template.

    Handles ``export`` prefixes, quotes and comment lines. Values are not
    expanded.

    Args:
        text: File contents.

    Returns:
        Mapping of variable name to (unquoted) value.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_]\w*", key.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def expand_home(value: str, home: Path) -> str:
    """Expand ``~``, ``$HOME`` and ``${HOME}`` in a path.

    Args:
        value: Path text.
        home: Home folder to substitute.

    Returns:
        The expanded text.
    """
    value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    return value


def project_names(pyproject: dict[str, Any], key: str) -> list[str]:
    """Read ``service_name`` or ``user_agent_name`` from a parsed pyproject.toml.

    Args:
        pyproject: Parsed pyproject.toml.
        key: Key under ``[project]`` to read.

    Returns:
        The names given (a string becomes a one-item list); empty if absent.
    """
    project = pyproject.get("project", {})
    value = project.get(key) if isinstance(project, dict) else None
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


# --------------------------------------------------------------------------
# Pure helpers: sudo and sudoers
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SudoCall:
    """A ``sudo`` command line found in a workflow.

    Attributes:
        runas: User the command runs as (``root`` unless ``-u`` is given).
        chdir: Folder given with ``--chdir`` / ``-D``, if any.
        command: Command to run.
        args: Its arguments.
    """

    runas: str
    chdir: str | None
    command: str
    args: tuple[str, ...]

    def __str__(self) -> str:
        """Show the call as a shell command line.

        Returns:
            The call, quoted for a shell.
        """
        parts = ["sudo"]
        if self.runas != "root":
            parts += ["-u", self.runas]
        if self.chdir:
            parts.append(f"--chdir={self.chdir}")
        parts += [self.command, *self.args]
        return shlex.join(parts)


@dataclass(frozen=True)
class SudoRule:
    """One command allowed by a sudoers rule.

    Attributes:
        runas: Users the command may run as (``ALL`` for anyone).
        cwd: ``CWD=`` option, if set (``*`` allows any ``--chdir``).
        nopasswd: Whether no password is needed.
        command: Allowed command (``ALL`` for any).
        args: Allowed arguments pattern; None allows any, ``""`` allows none.
        line: The sudoers line it came from.
    """

    runas: tuple[str, ...]
    cwd: str | None
    nopasswd: bool
    command: str
    args: str | None
    line: str


def split_commands(script: str) -> list[list[str]]:
    """Split a shell script into simple commands, each a list of words.

    Joins backslash-continued lines and splits on ``;``, ``&&``, ``||``, ``|``
    and ``&``. Lines that cannot be parsed are skipped.

    Args:
        script: Shell script text, e.g. a workflow step's ``run:``.

    Returns:
        One word list per simple command.
    """
    commands: list[list[str]] = []
    for raw in script.replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            continue
        current: list[str] = []
        for token in tokens:
            if token in SHELL_OPERATORS:
                if current:
                    commands.append(current)
                current = []
            else:
                current.append(token)
        if current:
            commands.append(current)
    return commands


def parse_sudo_call(words: list[str]) -> SudoCall | None:
    """Work out what a ``sudo ...`` command line asks sudo to run.

    Args:
        words: The command's words, starting with ``sudo``.

    Returns:
        The parsed call, or None if it is not a sudo call that runs a command.
    """
    if not words or Path(words[0]).name != "sudo":
        return None
    runas = "root"
    chdir: str | None = None
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--":
            i += 1
            break
        if word in {"-u", "--user", "-D", "--chdir"}:
            if i + 1 >= len(words):
                return None
            if word in {"-u", "--user"}:
                runas = words[i + 1]
            else:
                chdir = words[i + 1]
            i += 2
            continue
        if word.startswith("--user="):
            runas = word.split("=", 1)[1]
        elif word.startswith("--chdir="):
            chdir = word.split("=", 1)[1]
        elif word.startswith("-u") and len(word) > 2:
            runas = word[2:]
        elif word.startswith("-D") and len(word) > 2:
            chdir = word[2:]
        elif not word.startswith("-"):
            break
        i += 1
    if i >= len(words):
        return None
    command = words[i]
    if not command.startswith("/"):
        resolved = shutil.which(command)
        command = resolved or command
    return SudoCall(runas=runas, chdir=chdir, command=command, args=tuple(words[i + 1 :]))


def _sudoers_logical_lines(text: str) -> list[str]:
    """Join continued lines and drop comments from a sudoers file.

    Args:
        text: Sudoers file contents.

    Returns:
        The non-empty logical lines.
    """
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        stripped = line.strip()
        if not stripped or stripped.startswith(("#include", "@include")):
            continue
        if stripped.startswith("#"):
            continue
        lines.append(re.sub(r"(?<!\\)#.*$", "", stripped).strip())
    if pending.strip():
        lines.append(pending.strip())
    return [line for line in lines if line]


def parse_sudoers(text: str, user: str = RUNNER_USER) -> tuple[list[SudoRule], list[str]]:
    """Read the rules for ``user`` from a sudoers file.

    Supports the plain rule form used in the deployment guide
    (``user host=(runas) [CWD=x] [TAGS:] command [args], ...``). Aliases are
    not expanded.

    Args:
        text: Sudoers file contents.
        user: User whose rules to read.

    Returns:
        ``(rules, unsupported)``: one rule per allowed command, and the lines
        that could not be understood (aliases, or rules in another form).
    """
    rules: list[SudoRule] = []
    unsupported: list[str] = []
    for line in _sudoers_logical_lines(text):
        if line.startswith("Defaults"):
            continue
        if line.startswith(SUDO_ALIAS_KEYWORDS):
            unsupported.append(line)
            continue
        first, _, rest = line.partition(" ")
        if first != user:
            continue
        parsed = _parse_rule_spec(rest.strip(), line)
        if parsed is None:
            unsupported.append(line)
        else:
            rules.extend(parsed)
    return rules, unsupported


def _parse_rule_spec(spec: str, line: str) -> list[SudoRule] | None:
    """Parse the part of a sudoers rule after the user name.

    Args:
        spec: Text after the user, e.g. ``ALL=(ALL) NOPASSWD: /bin/ls``.
        line: The full line, kept on each rule.

    Returns:
        One rule per command, or None if the text is not understood.
    """
    _hosts, sep, rest = spec.partition("=")
    if not sep:
        return None
    rest = rest.strip()
    runas: tuple[str, ...] = ("root",)
    if rest.startswith("("):
        close = rest.find(")")
        if close < 0:
            return None
        users = rest[1:close].split(":", 1)[0]
        runas = tuple(u.strip() for u in users.split(",") if u.strip()) or ("root",)
        rest = rest[close + 1 :].strip()
    cwd: str | None = None
    nopasswd = False
    while True:
        if option := SUDO_OPTION_RE.match(rest):
            if option["name"] == "CWD":
                cwd = option["value"]
            rest = rest[option.end() :]
        elif tag := SUDO_TAG_RE.match(rest):
            if tag["tag"] == "NOPASSWD":
                nopasswd = True
            elif tag["tag"] == "PASSWD":
                nopasswd = False
            rest = rest[tag.end() :]
        else:
            break
    commands = [c.strip() for c in re.split(r"(?<!\\),", rest) if c.strip()]
    if not commands:
        return None
    rules: list[SudoRule] = []
    for command_spec in commands:
        command, _, args_text = command_spec.partition(" ")
        args: str | None = re.sub(r"\\([,:=\\ ])", r"\1", args_text.strip()) if args_text.strip() else None
        if args == '""':
            args = ""
        rules.append(SudoRule(runas, cwd, nopasswd, command, args, line))
    return rules


def rule_allows(rule: SudoRule, call: SudoCall) -> bool:
    """Check whether a sudoers rule lets the runner make a call without a password.

    Follows sudo's matching: arguments are matched as one string, so ``*``
    matches across spaces; ``--chdir`` needs ``CWD=*`` or the same folder.

    Args:
        rule: Allowed command.
        call: Call made by a workflow.

    Returns:
        True if sudo would run the call without asking for a password.
    """
    if not rule.nopasswd:
        return False
    if "ALL" not in rule.runas and call.runas not in rule.runas:
        return False
    if rule.command != "ALL" and not fnmatch.fnmatchcase(call.command, rule.command):
        return False
    if rule.command != "ALL" and rule.args is not None:
        if not rule.args:
            if call.args:
                return False
        elif not fnmatch.fnmatchcase(" ".join(call.args), rule.args):
            return False
    if call.chdir is not None:
        return rule.cwd in {"*", call.chdir}
    return True


def suggest_rule(call: SudoCall) -> str:
    """Suggest a sudoers line that allows ``call``.

    Args:
        call: A call that no rule allows.

    Returns:
        A sudoers line for the ``github-runner`` user.
    """
    runas = "ALL" if call.runas == "root" else call.runas
    cwd = "CWD=* " if call.chdir else ""
    if Path(call.command).name in {"servicectrl.sh", "gitrefresh.sh"}:
        args = " *"
    elif call.args:
        args = " " + " ".join(re.sub(r"([,:=\\])", r"\\\1", a) for a in call.args)
    else:
        args = ""
    return f"{RUNNER_USER} ALL=({runas}) {cwd}NOPASSWD: {call.command}{args}"


# --------------------------------------------------------------------------
# Pure helpers: deploy.yml
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class HostJob:
    """A deploy.yml job that runs on a given host.

    Attributes:
        job_id: Key of the job under ``jobs:``.
        job: The job's mapping.
        via: How it targets the host: ``"label"`` (``runs-on`` names the
            host) or ``"matrix"`` (``runs-on`` uses a matrix value or input
            that can be the host).
    """

    job_id: str
    job: dict[str, Any]
    via: str


def workflow_triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a workflow's ``on:`` mapping.

    PyYAML reads the bare key ``on`` as the boolean True, so both are tried.

    Args:
        workflow: Parsed workflow.

    Returns:
        The trigger mapping, or an empty dict.
    """
    raw: dict[Any, Any] = workflow
    triggers = raw.get("on", raw.get(True))
    return triggers if isinstance(triggers, dict) else {}


def environment_options(workflow: dict[str, Any]) -> list[str] | None:
    """Return the choices of the workflow_dispatch ``environment`` input.

    Args:
        workflow: Parsed workflow.

    Returns:
        The options, or None if the workflow has no such input.
    """
    dispatch = workflow_triggers(workflow).get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return None
    inputs = dispatch.get("inputs")
    env_input = inputs.get("environment") if isinstance(inputs, dict) else None
    options = env_input.get("options") if isinstance(env_input, dict) else None
    if not isinstance(options, list):
        return None
    return [str(o) for o in options]


def _runs_on_labels(job: dict[str, Any]) -> list[str]:
    """Return a job's ``runs-on`` labels.

    Args:
        job: Job mapping.

    Returns:
        The labels as strings.
    """
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [str(label) for label in runs_on]
    if isinstance(runs_on, dict) and isinstance(runs_on.get("labels"), list | str):
        labels = runs_on["labels"]
        return [labels] if isinstance(labels, str) else [str(label) for label in labels]
    return []


def _label_targets(label: str, job: dict[str, Any], host: str, options: list[str] | None) -> str | None:
    """Decide whether one ``runs-on`` label can be ``host``.

    Args:
        label: The label.
        job: The job it belongs to.
        host: Host name.
        options: The workflow's environment options.

    Returns:
        ``"label"``, ``"matrix"`` or None.
    """
    if label == host:
        return "label"
    expr = EXPRESSION_RE.fullmatch(label.strip())
    if not expr:
        return None
    if matrix_key := MATRIX_LABEL_RE.match(label.strip()):
        strategy = job.get("strategy")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        values = matrix.get(matrix_key["key"]) if isinstance(matrix, dict) else None
        if isinstance(values, list):
            return "matrix" if host in [str(v) for v in values] else None
        # A computed matrix (e.g. fromJSON of the chosen input) can be any option.
        return "matrix" if options and host in options else None
    if "inputs.environment" in expr["expr"]:
        return "matrix" if options and host in options else None
    return None


def jobs_for_host(workflow: dict[str, Any], host: str) -> list[HostJob]:
    """Find the self-hosted jobs in a workflow that can run on ``host``.

    Args:
        workflow: Parsed workflow.
        host: Host name (runner label), matched exactly.

    Returns:
        The matching jobs, in file order.
    """
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    options = environment_options(workflow)
    found: list[HostJob] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        labels = _runs_on_labels(job)
        if "self-hosted" not in labels:
            continue
        for label in labels:
            via = _label_targets(label, job, host, options)
            if via:
                found.append(HostJob(str(job_id), job, via))
                break
    return found


def runs_on_case_mismatch(workflow: dict[str, Any], host: str) -> list[str]:
    """Find ``runs-on`` labels that match ``host`` only when case is ignored.

    Args:
        workflow: Parsed workflow.
        host: Host name.

    Returns:
        The near-miss labels.
    """
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [
        label
        for job in jobs.values()
        if isinstance(job, dict)
        for label in _runs_on_labels(job)
        if label != host and label.lower() == host.lower()
    ]


def substitute(text: str, host: str, env: dict[str, str]) -> str:
    """Fill in the expressions and variables a deploy step uses.

    ``${{ github.ref_name }}`` becomes ``main`` (deploys are from main), and
    ``${{ matrix.<key> }}`` and ``${{ inputs.environment }}`` become the host
    (deploy workflows only use the matrix to pick the host). ``$NAME`` and ``${NAME}`` are replaced for names in ``env``.

    Args:
        text: Text to fill in.
        host: Host name.
        env: Step, job and workflow environment variables.

    Returns:
        The text with known values substituted.
    """

    def expression(match: re.Match[str]) -> str:
        expr = match["expr"]
        if expr == "github.ref_name":
            return "main"
        if expr.startswith("matrix.") or expr in {"inputs.environment", "github.event.inputs.environment"}:
            return host
        return match[0]

    text = EXPRESSION_RE.sub(expression, text)
    for name in sorted(env, key=len, reverse=True):
        value = EXPRESSION_RE.sub(expression, env[name])
        text = text.replace("${" + name + "}", value)
        text = re.sub(r"\$" + re.escape(name) + r"(?!\w)", value.replace("\\", "\\\\"), text)
    return text


def _step_applies(step: dict[str, Any], host: str) -> bool:
    """Evaluate a step's simple ``if: matrix.environment == 'X'`` condition.

    Args:
        step: Step mapping.
        host: Host name.

    Returns:
        False only when the condition clearly excludes ``host``.
    """
    condition = step.get("if")
    if not isinstance(condition, str):
        return True
    match = STEP_IF_RE.match(condition.strip())
    if not match:
        return True
    equal = match["value"] == host
    return equal if match["op"] == "==" else not equal


def _env_map(value: object) -> dict[str, str]:
    """Return an ``env:`` mapping with string values.

    Args:
        value: The ``env:`` value from a workflow, job or step.

    Returns:
        The variables, or an empty dict.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def job_commands(workflow: dict[str, Any], job: dict[str, Any], host: str) -> list[list[str]]:
    """List the simple shell commands a job runs on ``host``.

    Args:
        workflow: Parsed workflow (for its ``env:``).
        job: The job.
        host: Host name.

    Returns:
        Word lists, one per simple command, with values substituted.
    """
    base_env = {**_env_map(workflow.get("env")), **_env_map(job.get("env"))}
    commands: list[list[str]] = []
    steps = job.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or not isinstance(step.get("run"), str):
            continue
        if not _step_applies(step, host):
            continue
        env = {**base_env, **_env_map(step.get("env"))}
        commands.extend(split_commands(substitute(step["run"], host, env)))
    return commands


def job_sudo_calls(workflow: dict[str, Any], job: dict[str, Any], host: str) -> list[SudoCall]:
    """List the sudo calls a job makes on ``host``.

    Args:
        workflow: Parsed workflow.
        job: The job.
        host: Host name.

    Returns:
        The parsed sudo calls.
    """
    calls: list[SudoCall] = []
    for words in job_commands(workflow, job, host):
        call = parse_sudo_call(words)
        if call is not None:
            calls.append(call)
    return calls


def job_uses_script(workflow: dict[str, Any], job: dict[str, Any], host: str, script: str) -> bool:
    """Check whether a job runs a given script (directly or through sudo).

    Args:
        workflow: Parsed workflow.
        job: The job.
        host: Host name.
        script: Script file name, e.g. ``servicectrl.sh``.

    Returns:
        True if any command word is that script.
    """
    return any(Path(word).name == script for words in job_commands(workflow, job, host) for word in words)


def job_environment(job: dict[str, Any], host: str) -> str | None:
    """Return the GitHub Environment a job deploys to on ``host``.

    Args:
        job: The job.
        host: Host name (substituted for matrix expressions).

    Returns:
        The environment name, or None if the job sets none.
    """
    env = job.get("environment")
    if isinstance(env, dict):
        env = env.get("name")
    if not isinstance(env, str):
        return None
    return substitute(env, host, {})


# --------------------------------------------------------------------------
# Pure helpers: auto-deploy.yaml
# --------------------------------------------------------------------------
@dataclass
class AutoDeployReview:
    """Findings for a project's ``deploy/auto-deploy.yaml``.

    Attributes:
        errors: Problems that make the file invalid.
        warnings: Things that are valid but probably not intended.
        summary: Short description of what the file asks for.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""


def review_auto_deploy(data: object, options: list[str] | None, host: str) -> AutoDeployReview:
    """Check an auto-deploy control file the way the auto-deploy orchestrator does.

    Follows ``src/control_file.py`` in the automation repo: unknown settings
    are rejected, ``Environments`` is the string ``all`` or a list of
    deploy.yml options (matched without regard to case), and the schedule has
    exactly one of ``Interval`` and ``At``.

    Args:
        data: The parsed YAML.
        options: The environment options of the project's deploy.yml, if known.
        host: This host's name.

    Returns:
        The errors, warnings and a summary.
    """
    review = AutoDeployReview()
    if not isinstance(data, dict):
        review.errors.append("the file must be a mapping of settings (Enabled, Environments, Schedule, ...)")
        return review

    review.errors.extend(f"unknown setting '{key}'; auto-deploy rejects the file" for key in data if key not in AUTO_DEPLOY_KEYS)

    enabled = data.get("Enabled")
    if "Enabled" not in data:
        review.errors.append("Enabled is required (true or false)")
    elif not isinstance(enabled, bool):
        review.errors.append(f"Enabled must be true or false, not {enabled!r}")
    elif not enabled:
        review.warnings.append("Enabled is false, so nothing is deployed automatically")

    environments = _review_environments(data, options, host, review)
    schedule = _review_schedule(data.get("Schedule"), review)

    timezone = data.get("TimeZone") or DEFAULT_TIMEZONE
    if not isinstance(timezone, str):
        review.errors.append(f"TimeZone must be a name such as {DEFAULT_TIMEZONE}, not {timezone!r}")
    else:
        try:
            zoneinfo.ZoneInfo(timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            review.errors.append(f"TimeZone '{timezone}' is not a known time zone")

    if not review.errors:
        state = "enabled" if enabled else "disabled"
        review.summary = f"{state}; {environments}; {schedule}; time zone {timezone}"
    return review


def _review_environments(data: dict[str, Any], options: list[str] | None, host: str, review: AutoDeployReview) -> str:
    """Check the Environments setting.

    Args:
        data: The parsed control file.
        options: The deploy.yml environment options, if known.
        host: This host's name.
        review: Findings to add to.

    Returns:
        A short description for the summary.
    """
    if "Environments" not in data:
        review.errors.append("Environments is required (all, or a list of deploy.yml environment options)")
        return ""
    value = data["Environments"]
    if value == "all":
        if options is not None and host.lower() not in {o.lower() for o in options}:
            review.warnings.append(f"this host ({host}) is not a deploy.yml option, so 'all' does not include it")
        return "all hosts"
    if not isinstance(value, list) or not value or not all(isinstance(h, str) and h for h in value):
        review.errors.append(f"Environments must be all, or a list of deploy.yml environment options, not {value!r}")
        return ""
    names = [str(h) for h in value]
    if options is not None:
        lowered = {o.lower() for o in options}
        unknown = [n for n in names if n.lower() not in lowered]
        if unknown:
            review.errors.append(f"Environments {', '.join(unknown)} not an option in deploy.yml ({', '.join(options)})")
    wanted = {n.lower() for n in names}
    if host.lower() not in wanted and not wanted & PSEUDO_ENVIRONMENTS:
        review.warnings.append(f"Environments does not include this host ({host}), so it is not auto-deployed here")
    return "environments " + ", ".join(names)


def _review_schedule(schedule: object, review: AutoDeployReview) -> str:
    """Check the Schedule setting.

    Args:
        schedule: The Schedule value.
        review: Findings to add to.

    Returns:
        A short description for the summary.
    """
    if not isinstance(schedule, dict):
        review.errors.append("Schedule is required, with either Interval or At")
        return ""
    review.errors.extend(f"unknown Schedule setting '{key}'; auto-deploy rejects the file" for key in schedule if key not in {"Interval", "At"})
    interval, times = schedule.get("Interval"), schedule.get("At")
    if (interval is None) == (times is None):
        review.errors.append("Schedule must have exactly one of Interval or At")
        return ""
    if interval is not None:
        if not isinstance(interval, str) or interval.strip().lower() not in AUTO_DEPLOY_INTERVALS:
            review.errors.append(f"Schedule.Interval must be one of {', '.join(AUTO_DEPLOY_INTERVALS)}, not {interval!r}")
            return ""
        return f"every {interval.strip().lower()}"
    if not isinstance(times, list) or not times:
        review.errors.append('Schedule.At must be a list of quoted times, e.g. ["02:00"]')
        return ""
    for value in times:
        if not isinstance(value, str):
            review.errors.append(f'Schedule.At has an unquoted time that YAML read as the number {value!r}; quote it, e.g. ["14:30"]')
        elif not TIME_RE.match(value.strip()):
            review.errors.append(f"Schedule.At time '{value}' is not a 24-hour HH:MM time")
    return "at " + ", ".join(str(t) for t in times)


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------
@dataclass
class Context:
    """Facts about where the checks run.

    Attributes:
        system: ``"linux"`` or ``"macos"``.
        host: Host name (runner name and label).
        host_source: Where the host name came from.
        user: Deploy user (``<User>``), who owns ``~/scripts``.
        uid: The deploy user's user id.
        home: The deploy user's home folder.
        project: Project folder to check, or None for host checks only.
        online: Whether network checks (git ls-remote, gh) may run.
        use_sudo: Whether ``sudo -n`` may be used.
        script_dir: Folder this script was run from.
        runner_dir: Where the Actions runner is installed.
        runner_dir_source: How ``runner_dir`` was found.
    """

    system: str
    host: str
    host_source: str
    user: str
    uid: int
    home: Path
    project: Path | None
    online: bool
    use_sudo: bool
    script_dir: Path
    runner_dir: Path
    runner_dir_source: str
    _sudoers: tuple[list[SudoRule], list[str], str] | None = None

    @property
    def scripts_dir(self) -> Path:
        """The folder that holds the deployed clones (``~/scripts``)."""
        return self.home / "scripts"

    @property
    def pyutil_dir(self) -> Path:
        """The deployed pyutil clone (``~/scripts/pyutil``)."""
        return self.scripts_dir / "pyutil"

    def t(self, path: Path | str) -> str:
        """Show a path with the home folder as ``~``.

        Args:
            path: Path to show.

        Returns:
            The abbreviated path.
        """
        return tilde(path, self.home)

    def sudoers(self) -> tuple[list[SudoRule], list[str], str]:
        """Read and parse the runner's sudoers file once.

        Returns:
            ``(rules, unsupported_lines, problem)`` where problem is empty on
            success, else ``"missing"``, ``"needs sudo"`` or an error.
        """
        if self._sudoers is None:
            text, problem = read_text(SUDOERS_FILE, use_sudo=self.use_sudo)
            if text is None:
                self._sudoers = ([], [], problem)
            else:
                rules, unsupported = parse_sudoers(text)
                self._sudoers = (rules, unsupported, "")
        return self._sudoers


def default_runner_dir(system: str, home: Path) -> Path:
    """Return where the deployment guide installs the runner.

    Args:
        system: ``"linux"`` or ``"macos"``.
        home: Deploy user's home.

    Returns:
        The documented runner folder.
    """
    return LINUX_RUNNER_DIR if system == "linux" else home / "actions-runner"


def runner_service_files(system: str, home: Path) -> list[Path]:
    """List the runner service definitions installed by ``svc.sh install``.

    Args:
        system: ``"linux"`` or ``"macos"``.
        home: Deploy user's home.

    Returns:
        systemd units (Linux) or LaunchAgent plists (macOS) for this organisation's runners.
    """
    if system == "linux":
        return sorted(SYSTEMD_DIR.glob(f"actions.runner.{ORG}.*.service"))
    return sorted((home / "Library/LaunchAgents").glob(f"actions.runner.{ORG}.*.plist"))


def service_working_directory(path: Path) -> Path | None:
    """Read the runner folder from a runner's systemd unit or LaunchAgent plist.

    Args:
        path: A file from ``runner_service_files``.

    Returns:
        Its working directory, or None if it cannot be read.
    """
    try:
        if path.suffix == ".plist":
            data = plistlib.loads(path.read_bytes())
            folder = data.get("WorkingDirectory") if isinstance(data, dict) else None
            return Path(folder) if isinstance(folder, str) else None
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key == "WorkingDirectory" and value.strip():
                return Path(value.strip())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return None


def find_runner_dir(system: str, home: Path, host: str | None) -> tuple[Path, str]:
    """Find where the runner is installed, from its service, else the guide's location.

    Args:
        system: ``"linux"`` or ``"macos"``.
        home: Deploy user's home.
        host: Host name from ``--host``, used to pick a runner if there are several.

    Returns:
        ``(folder, how it was found)``.
    """
    services = runner_service_files(system, home)
    if host:
        named = [s for s in services if s.stem.removesuffix(".service") == f"actions.runner.{ORG}.{host}"]
        services = named or services
    for service in services:
        folder = service_working_directory(service)
        if folder is not None:
            return folder, f"from {tilde(service, home)}"
    return default_runner_dir(system, home), "default location"


def read_runner_config(runner_dir: Path, *, use_sudo: bool) -> tuple[dict[str, Any] | None, str]:
    """Read the runner's ``.runner`` file (written by ``config.sh``).

    Args:
        runner_dir: Runner install folder.
        use_sudo: Whether ``sudo -n`` may be used.

    Returns:
        ``(config, "")`` or ``(None, reason)``.
    """
    text, problem = read_text(runner_dir / ".runner", use_sudo=use_sudo)
    if text is None:
        return None, problem
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON ({exc})"
    return (data, "") if isinstance(data, dict) else (None, "not a JSON object")


def load_yaml(path: Path) -> tuple[Any, str]:
    """Load a YAML file.

    Args:
        path: File to load.

    Returns:
        ``(data, "")`` or ``(None, error)``.
    """
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "missing"
    except (OSError, yaml.YAMLError) as exc:
        return None, f"cannot be read: {exc}"


def load_pyproject(project: Path) -> dict[str, Any]:
    """Load a project's pyproject.toml.

    Args:
        project: Project folder.

    Returns:
        The parsed file, or an empty dict if it is missing or invalid.
    """
    try:
        with (project / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# --------------------------------------------------------------------------
# Host checks
# --------------------------------------------------------------------------
def check_host_common(ctx: Context, sec: Section, runner: dict[str, Any] | None, runner_problem: str) -> None:
    """Checks for every host: tools, the pyutil clone and the runner registration.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        runner: Parsed ``.runner`` file, if it could be read.
        runner_problem: Why it could not be read.
    """
    if os.geteuid() == 0:
        sec.warn("Running as the deploy user", f"running as root; checks are meant to run as {ctx.user}", f"run it again without sudo, as {ctx.user}")

    missing = [tool for tool in ("git", "curl") if which(tool) is None]
    if missing:
        install = "sudo apt install " if ctx.system == "linux" else "brew install "
        sec.fail("git and curl are installed", f"missing: {', '.join(missing)}", install + " ".join(missing))
    else:
        sec.ok("git and curl are installed")

    check_pyutil_clone(ctx, sec)
    check_runner_registration(ctx, sec, runner, runner_problem)


def check_pyutil_clone(ctx: Context, sec: Section) -> None:
    """Check the deployed pyutil clone at ``~/scripts/pyutil``.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    pyutil = ctx.pyutil_dir
    clone = f"mkdir -p ~/scripts && git clone {ORG_URL}/pyutil.git ~/scripts/pyutil"
    if not (pyutil / ".git").exists():
        sec.fail("pyutil is cloned to ~/scripts/pyutil", f"{ctx.t(pyutil)} is not a git clone", clone)
        return
    remote = git(pyutil, "remote", "get-url", "origin")
    parsed = parse_github_remote(remote.stdout) if remote.ok else None
    if parsed is None or parsed[0].lower() != ORG.lower() or parsed[1].lower() != "pyutil":
        sec.fail("pyutil is cloned to ~/scripts/pyutil", f"origin is '{remote.stdout.strip() or remote.stderr.strip()}', expected {ORG_URL}/pyutil.git", f"git -C ~/scripts/pyutil remote set-url origin {ORG_URL}/pyutil.git")
    else:
        sec.ok("pyutil is cloned to ~/scripts/pyutil", remote.stdout.strip())

    if ctx.script_dir.resolve() != pyutil.resolve():
        sec.warn("Running from ~/scripts/pyutil", f"this script is in {ctx.t(ctx.script_dir)}; run the deployed copy so the checks match what deploys use", "~/scripts/pyutil/deploy_check.sh")

    check_branch_and_changes(ctx, sec, pyutil, "pyutil")
    if ctx.online:
        check_up_to_date(ctx, sec, pyutil, "pyutil", behind_is_warning=True)

    scripts = ["gitrefresh.sh", "git_dev_env_check.sh", "servicectrl.sh" if ctx.system == "linux" else "useragentctrl.sh"]
    problems = []
    for name in scripts:
        path = pyutil / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif name != "git_dev_env_check.sh" and not os.access(path, os.X_OK):
            problems.append(f"{name} is not executable")
    if problems:
        sec.fail("Deploy scripts are present", problems, "deploy pyutil to this host, or: git -C ~/scripts/pyutil reset --hard origin/main")
    else:
        sec.ok("Deploy scripts are present", ", ".join(scripts))

    check_ownership(ctx, sec, pyutil, "pyutil")
    check_pyutil_workflow(ctx, sec)


def check_branch_and_changes(ctx: Context, sec: Section, repo: Path, name: str) -> None:
    """Check that a clone is on main with no changes to tracked files.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        repo: The clone.
        name: Name to show.
    """
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch.ok:
        sec.fail(f"{name} is on main", branch.stderr.strip() or "git cannot read the clone")
        return
    if branch.stdout.strip() != "main":
        sec.warn(f"{name} is on main", f"on '{branch.stdout.strip()}'; auto-deploy skips hosts that are not on main", f"cd {ctx.t(repo)} && git checkout main")
    else:
        sec.ok(f"{name} is on main")
    status = git(repo, "status", "--porcelain", "--untracked-files=no")
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if not status.ok:
        sec.warn(f"{name} has no local changes", status.stderr.strip())
    elif changed:
        sec.warn(f"{name} has no local changes", ["tracked files were changed here; the next deploy stashes or discards them:", *changed[:OWNERSHIP_SAMPLE]], f"cd {ctx.t(repo)} && git diff   # then commit them upstream, or: git checkout -- .")
    else:
        sec.ok(f"{name} has no local changes")


def check_up_to_date(ctx: Context, sec: Section, repo: Path, name: str, *, behind_is_warning: bool) -> None:
    """Check that origin can be read without a prompt, and compare with origin/main.

    Uses ``git ls-remote`` so nothing in the clone is changed.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        repo: The clone.
        name: Name to show.
        behind_is_warning: Warn (rather than pass) when origin/main is newer.
    """
    remote = git(repo, "ls-remote", "origin", "refs/heads/main", timeout=30)
    if not remote.ok:
        last = remote.stderr.strip().splitlines()[-1:] or ["git ls-remote failed"]
        sec.fail(f"{name}: origin can be read without a password", last, "set up git credentials for this repo (see the git-credentials page in the deployment docs)")
        return
    if not remote.stdout.strip():
        sec.fail(f"{name}: origin can be read without a password", "origin has no main branch")
        return
    sec.ok(f"{name}: origin can be read without a password")
    remote_sha = remote.stdout.split()[0]
    local_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    if local_sha == remote_sha:
        sec.ok(f"{name} matches origin/main", remote_sha[:10])
        return
    detail = f"local {local_sha[:10] or '?'}, origin/main {remote_sha[:10]}"
    if behind_is_warning:
        sec.warn(f"{name} matches origin/main", detail, f"gh workflow run deploy.yml --repo {ORG}/{name} -f environment={ctx.host}")
    else:
        sec.ok(f"{name} matches origin/main", f"{detail}; the next deploy will update it")


def check_ownership(ctx: Context, sec: Section, repo: Path, name: str) -> None:
    """Check that the deploy user owns a clone and everything in it.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        repo: The clone.
        name: Name to show.
    """
    wrong = find_foreign_owned(repo, ctx.uid)
    title = f"{name} files are owned by {ctx.user}"
    if wrong:
        sec.fail(title, [f"not owned by {ctx.user} (first {len(wrong)} found):", *wrong], f"sudo chown -R {ctx.user} {ctx.t(repo)}")
    else:
        sec.ok(title)


def check_pyutil_workflow(ctx: Context, sec: Section) -> None:
    """Check that pyutil's own deploy.yml can deploy pyutil to this host.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    workflow, problem = load_yaml(ctx.pyutil_dir / WORKFLOW)
    title = f"pyutil's deploy.yml deploys to {ctx.host}"
    if not isinstance(workflow, dict):
        sec.fail(title, f"{ctx.t(ctx.pyutil_dir / WORKFLOW)} {problem or 'is not a mapping'}")
        return
    options = environment_options(workflow) or []
    jobs = jobs_for_host(workflow, ctx.host)
    problems = []
    if ctx.host not in options:
        problems.append(f"'{ctx.host}' is not an environment option ({', '.join(options) or 'none'})")
    if not jobs:
        problems.append(f"no job has runs-on: [self-hosted, {ctx.host}]")
    if problems:
        sec.fail(title, problems, "add the host to pyutil's .github/workflows/deploy.yml (in the pyutil repo)")
    else:
        sec.ok(title, f"job {jobs[0].job_id}")


def check_runner_registration(ctx: Context, sec: Section, runner: dict[str, Any] | None, runner_problem: str) -> None:
    """Check the runner's ``.runner`` file: name, organisation and host.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        runner: Parsed ``.runner`` file.
        runner_problem: Why it could not be read.
    """
    title = f"Runner is registered as {ctx.host} with {ORG}"
    if runner is None:
        if runner_problem == "needs sudo":
            sec.warn(title, f"could not check (needs sudo to read {ctx.runner_dir / '.runner'})", "run again after: sudo -v")
        else:
            sec.fail(title, f"{ctx.t(ctx.runner_dir / '.runner')} is {runner_problem}; the runner is not installed or configured", f"register a runner: {DOCS_URL}")
        return
    name = str(runner.get("agentName", ""))
    url = str(runner.get("gitHubUrl", "")).rstrip("/")
    problems = []
    if name != ctx.host:
        problems.append(f"runner name is '{name}', expected '{ctx.host}' (case matters)")
    if url.lower() != ORG_URL.lower():
        problems.append(f"registered to {url or '?'}, expected the organisation {ORG_URL}")
    if problems:
        sec.fail(title, problems, "remove and re-register the runner (./config.sh remove, then ./config.sh) with the right name")
    else:
        where = ctx.t(ctx.runner_dir)
        if ctx.runner_dir != default_runner_dir(ctx.system, ctx.home):
            where += f" (the guide uses {ctx.t(default_runner_dir(ctx.system, ctx.home))}; found {ctx.runner_dir_source})"
        sec.ok(title, f"name {name}, in {where}")

    short = socket.gethostname().split(".", 1)[0]
    if ctx.host_source == "runner" and short.lower() != ctx.host.lower():
        sec.skip("Host name matches the runner", f"hostname -s is '{short}'; using the runner name '{ctx.host}'")


def check_runner_online(ctx: Context, sec: Section) -> None:
    """Ask GitHub whether the runner is online with the right label.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    title = f"GitHub shows runner {ctx.host} online with label {ctx.host}"
    if not ctx.online:
        sec.skip(title, "--offline")
        return
    if which("gh") is None:
        sec.skip(title, "gh is not installed")
        return
    result = run(
        ["gh", "api", "--paginate", f"orgs/{ORG}/actions/runners", "--jq", ".runners[] | {name, status, labels: [.labels[].name]}"],
        timeout=30,
    )
    if not result.ok:
        reason = (result.stderr.strip().splitlines() or ["unknown error"])[0]
        sec.skip(title, ["gh could not list the organisation's runners (needs an org admin login with the admin:org scope):", reason])
        return
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("name") != ctx.host:
            continue
        problems = []
        if entry.get("status") != "online":
            problems.append(f"status is {entry.get('status')}")
        if ctx.host not in entry.get("labels", []):
            problems.append(f"labels are {', '.join(entry.get('labels', []))}; '{ctx.host}' is missing (case matters)")
        if problems:
            sec.fail(title, problems, f"check the runner service, and the labels under {ORG_URL} → Settings → Actions → Runners")
        else:
            sec.ok(title, "labels: " + ", ".join(entry.get("labels", [])))
        return
    sec.fail(title, f"no runner named {ctx.host} in {ORG}", f"register a runner: {DOCS_URL}")


def check_host_linux(ctx: Context, sec: Section) -> None:
    """Checks from "Set up a new Linux host".

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    check_runner_user(ctx, sec)
    check_linux_runner_service(ctx, sec)
    check_linux_uv(ctx, sec)
    check_safe_directory(sec, ctx.pyutil_dir, "pyutil")
    check_sudoers(ctx, sec)


def check_runner_user(ctx: Context, sec: Section) -> None:
    """Check that the github-runner user exists and has a locked password.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    try:
        pwd.getpwnam(RUNNER_USER)
    except KeyError:
        sec.fail(f"User {RUNNER_USER} exists", "", [f"sudo useradd -m -s /bin/bash {RUNNER_USER}", f"sudo passwd -l {RUNNER_USER}"])
        return
    sec.ok(f"User {RUNNER_USER} exists")
    title = f"{RUNNER_USER}'s password is locked"
    if not ctx.use_sudo:
        sec.warn(title, "could not check (needs sudo)")
        return
    result = run(["sudo", "-n", "passwd", "-S", RUNNER_USER])
    fields = result.stdout.split()
    if not result.ok or len(fields) < 2:
        sec.warn(title, "could not check (needs sudo)", "run again after: sudo -v")
    elif fields[1] in {"L", "LK"}:
        sec.ok(title)
    else:
        sec.fail(title, f"password status is '{fields[1]}'", f"sudo passwd -l {RUNNER_USER}")


def check_linux_runner_service(ctx: Context, sec: Section) -> None:
    """Check the runner's systemd service is enabled, active and runs as github-runner.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    text, _ = read_text(ctx.runner_dir / ".service", use_sudo=ctx.use_sudo)
    unit = text.strip() if text else f"actions.runner.{ORG}.{ctx.host}.service"
    install = [
        f'sudo sh -c "cd {ctx.runner_dir} && ./svc.sh install {RUNNER_USER}"',
        f'sudo sh -c "cd {ctx.runner_dir} && ./svc.sh start"',
    ]
    active = run(["systemctl", "is-active", unit]).stdout.strip()
    enabled = run(["systemctl", "is-enabled", unit]).stdout.strip()
    title = "Runner service is running"
    if active == "active" and enabled == "enabled":
        sec.ok(title, unit)
    elif enabled in {"", "not-found"}:
        sec.fail(title, f"{unit} is not installed", install)
    else:
        sec.fail(title, f"{unit} is {active} / {enabled}", [f"sudo systemctl enable --now {unit}", f"sudo journalctl -u {unit} -n 50"])
    runs_as = run(["systemctl", "show", "-p", "User", "--value", unit]).stdout.strip()
    if runs_as and runs_as != RUNNER_USER:
        sec.fail(f"Runner service runs as {RUNNER_USER}", f"it runs as '{runs_as}'", install)


def check_linux_uv(ctx: Context, sec: Section) -> None:
    """Check uv is installed for the deploy user and linked for root.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    user_uv = ctx.home / ".local/bin/uv"
    if user_uv.is_file() and os.access(user_uv, os.X_OK):
        sec.ok(f"uv is installed for {ctx.user}", ctx.t(user_uv))
    elif (found := which("uv")) is not None:
        sec.warn(f"uv is installed for {ctx.user}", f"found {found}, not ~/.local/bin/uv where the guide installs it")
    else:
        sec.fail(f"uv is installed for {ctx.user}", "uv not found", "curl -LsSf https://astral.sh/uv/install.sh | sh")

    title = "uv can be found by root"
    fix = f"sudo ln -sf {user_uv} {ROOT_UV}"
    if not ROOT_UV.exists():
        sec.fail(title, f"{ROOT_UV} is missing (gitrefresh.sh runs as root and calls uv sync)", fix)
    elif ROOT_UV.resolve() != user_uv.resolve():
        sec.warn(title, f"{ROOT_UV} points to {ROOT_UV.resolve()}, not {ctx.t(user_uv)}", fix)
    else:
        sec.ok(title, f"{ROOT_UV} → {ctx.t(user_uv)}")


def check_safe_directory(sec: Section, repo: Path, name: str) -> None:
    """Check that git's system config marks a clone as a safe directory.

    Args:
        sec: Section to add results to.
        repo: The clone.
        name: Name to show.
    """
    result = run(["git", "config", "--system", "--get-all", "safe.directory"])
    entries = {line.strip() for line in result.stdout.splitlines()}
    title = f"{name} is a git safe.directory"
    if str(repo) in entries or "*" in entries:
        sec.ok(title)
    else:
        sec.fail(title, f"{repo} is not in git's system safe.directory list", f"sudo git config --system --add safe.directory {repo}")


def check_sudoers(ctx: Context, sec: Section) -> None:
    """Check /etc/sudoers.d/github-runner allows what deploys run.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    title = f"{SUDOERS_FILE} is valid"
    rules, unsupported, problem = ctx.sudoers()
    if problem == "missing":
        sec.fail(title, "the file is missing", f"sudo visudo -f {SUDOERS_FILE}   # add the lines from the Linux host guide")
        return
    if problem:
        sec.warn(title, f"could not check ({problem})", "run again after: sudo -v")
        return
    valid = run(["sudo", "-n", "visudo", "-c", "-f", str(SUDOERS_FILE)])
    if valid.ok:
        sec.ok(title)
    else:
        sec.fail(title, (valid.stdout + valid.stderr).strip().splitlines(), f"sudo visudo -f {SUDOERS_FILE}")
    if unsupported:
        sec.warn("sudoers rules can be checked", ["these lines use aliases or a form this check does not read:", *unsupported])

    pyutil = ctx.pyutil_dir
    example = str(ctx.scripts_dir / "<Project>")
    needed = [
        SudoCall("root", example, str(pyutil / "servicectrl.sh"), ("stop",)),
        SudoCall("root", example, str(pyutil / "gitrefresh.sh"), ("--yes", "--branch", "main")),
    ]
    workflow, _ = load_yaml(pyutil / WORKFLOW)
    if isinstance(workflow, dict):
        for job in jobs_for_host(workflow, ctx.host):
            needed.extend(job_sudo_calls(workflow, job.job, ctx.host))
    report_sudo_calls(ctx, sec, "sudoers allows the pyutil deploy and the deploy scripts", needed, rules)
    check_legacy_pull_rules(ctx, sec, rules)


def report_sudo_calls(ctx: Context, sec: Section, title: str, calls: list[SudoCall], rules: list[SudoRule]) -> None:
    """Report which sudo calls the runner's sudoers rules do not allow.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        title: Check title.
        calls: Calls a deploy makes.
        rules: Parsed rules for github-runner.
    """
    blocked = [call for call in calls if not any(rule_allows(rule, call) for rule in rules)]
    foreign = sorted({p for call in calls for p in _paths_outside_home(call, ctx.home)} | {f"runs as '{call.runas}'" for call in calls if call.runas not in {"root", ctx.user}})
    if foreign:
        sec.warn(f"{title}: paths and users match this host", [f"the workflow refers to another user's files ({ctx.user} owns {ctx.t(ctx.scripts_dir)}):", *foreign], "fix the paths and -u user in the workflow for this host")
    if blocked:
        suggestions = list(dict.fromkeys(suggest_rule(call) for call in blocked))
        sec.fail(title, ["not allowed without a password:", *(str(c) for c in blocked)], [f"sudo visudo -f {SUDOERS_FILE}   # and add:", *suggestions])
    else:
        sec.ok(title, f"{len(calls)} sudo call(s) checked")


def _paths_outside_home(call: SudoCall, home: Path) -> list[str]:
    """Find home-folder paths in a sudo call that belong to a different user.

    Args:
        call: The call.
        home: This host's deploy user's home.

    Returns:
        The offending paths.
    """
    words = [call.command, *(call.args), *([call.chdir] if call.chdir else [])]
    return [w for w in words if w.startswith(("/home/", "/Users/")) and not (w == str(home) or w.startswith(f"{home}/"))]


def check_legacy_pull_rules(ctx: Context, sec: Section, rules: list[SudoRule]) -> None:
    """Warn about ``git -C <project> pull`` sudoers lines that no workflow uses now.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        rules: Parsed rules for github-runner.
    """
    stale = []
    for rule in rules:
        match = re.fullmatch(r"-C (\S+) pull", rule.args or "")
        if not match or Path(rule.command).name != "git":
            continue
        project = Path(match[1])
        workflow, _ = load_yaml(project / WORKFLOW)
        uses_pull = False
        if isinstance(workflow, dict):
            for job in jobs_for_host(workflow, ctx.host):
                calls = job_sudo_calls(workflow, job.job, ctx.host)
                uses_pull = uses_pull or any(rule_allows(rule, call) and not call.chdir for call in calls)
        if not uses_pull:
            stale.append(rule.line)
    if stale:
        sec.warn("No unused legacy 'git pull' sudoers lines", ["these lines are for workflows that now use gitrefresh.sh (or projects not on this host):", *stale], f"sudo visudo -f {SUDOERS_FILE}   # and remove them")


def check_host_macos(ctx: Context, sec: Section) -> None:
    """Checks from "Set up a new macOS host".

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    brew = which("brew", Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew"))
    if brew is None:
        sec.fail("Homebrew is installed", "brew not found", "install Homebrew from https://brew.sh")
        prefix = None
    else:
        prefix = brew.resolve().parent.parent
        sec.ok("Homebrew is installed", str(brew))

    for tool in ("git", "uv"):
        found = which(tool)
        if found is None:
            sec.fail(f"{tool} is installed", f"{tool} not found", f"brew install {tool}")
        elif prefix is not None and not str(found.resolve()).startswith(str(prefix)):
            sec.warn(f"{tool} comes from Homebrew", f"using {found}; the guide installs it with brew", f"brew install {tool}")
        else:
            sec.ok(f"{tool} is installed", str(found))

    check_macos_runner_service(ctx, sec)
    check_runner_path(ctx, sec)


def check_macos_runner_service(ctx: Context, sec: Section) -> None:
    """Check the runner's LaunchAgent is installed, loaded and running.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    title = "Runner service is running"
    label = f"actions.runner.{ORG}.{ctx.host}"
    plist = ctx.home / "Library/LaunchAgents" / f"{label}.plist"
    runner = ctx.t(ctx.runner_dir)
    fix = [f"cd {runner} && ./svc.sh install", "./svc.sh start"]
    if not plist.is_file():
        sec.fail(title, f"{ctx.t(plist)} is missing", fix)
        return
    listing = run(["launchctl", "list"])
    for line in listing.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[2] == label:
            if fields[0] == "-":
                sec.fail(title, f"{label} is loaded but not running (last exit {fields[1]})", [f"cd {runner} && ./svc.sh stop && ./svc.sh start", f"tail ~/Library/Logs/{label}/stderr.log"])
            else:
                sec.ok(title, f"{label}, pid {fields[0]}")
            return
    sec.fail(title, f"{label} is not loaded", [f"cd {runner} && ./svc.sh start"])


def check_runner_path(ctx: Context, sec: Section) -> None:
    """Check the runner's saved PATH includes the folders holding uv and git.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
    """
    title = "Runner's PATH includes uv and git"
    path_file = ctx.runner_dir / ".path"
    text, problem = read_text(path_file, use_sudo=False)
    if text is None:
        sec.fail(title, f"{ctx.t(path_file)} is {problem}", f"cd {ctx.t(ctx.runner_dir)} && ./svc.sh install")
        return
    dirs = {d.rstrip("/") for d in text.strip().split(":") if d}
    missing = []
    for tool in ("uv", "git"):
        found = which(tool)
        if found is not None and str(found.parent) not in dirs:
            missing.append(f"{tool} is in {found.parent}, which is not in {ctx.t(path_file)}")
    if missing:
        sec.fail(title, missing, [f"add the folder(s) to {ctx.t(path_file)}, then:", f"cd {ctx.t(ctx.runner_dir)} && ./svc.sh stop && ./svc.sh start"])
    else:
        sec.ok(title)


# --------------------------------------------------------------------------
# Project checks
# --------------------------------------------------------------------------
def check_project(ctx: Context, sec: Section, project: Path) -> None:
    """Checks from "Add a project to a host" and "Automatic deployments".

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
    """
    name = project.name
    dev = [m for m in DEV_MARKERS if (project / m).exists()] + [p for p in DEV_PATH_PATTERNS if p in str(project)]
    if dev:
        sec.warn("Folder is a deployed clone", f"looks like a dev checkout ({', '.join(dev)}); gitrefresh.sh refuses to run here")

    if project.parent != ctx.scripts_dir:
        sec.fail(f"Project is in ~/scripts/{name}", f"it is in {ctx.t(project)}", f"git clone {ORG_URL}/{name}.git ~/scripts/{name}")
    else:
        sec.ok(f"Project is in ~/scripts/{name}")

    remote = git(project, "remote", "get-url", "origin")
    parsed = parse_github_remote(remote.stdout) if remote.ok else None
    if parsed is None:
        sec.fail(f"origin is {ORG}/{name}", f"origin is '{remote.stdout.strip() or 'not set'}', not a GitHub repository", f"git remote set-url origin {ORG_URL}/{name}.git")
    elif parsed[0].lower() != ORG.lower():
        sec.warn(f"origin is {ORG}/{name}", f"origin is {parsed[0]}/{parsed[1]}; this works only while GitHub redirects the old name", f"cd {ctx.t(project)} && git remote set-url origin {ORG_URL}/{parsed[1]}.git")
    elif parsed[1].lower() != name.lower():
        sec.warn(f"origin is {ORG}/{name}", f"origin is {parsed[0]}/{parsed[1]}; the folder name differs, and deploy.yml paths use the folder name")
    else:
        sec.ok(f"origin is {ORG}/{name}")

    if ctx.system == "linux":
        check_safe_directory(sec, project, name)
    check_ownership(ctx, sec, project, name)
    check_branch_and_changes(ctx, sec, project, name)
    if ctx.online:
        check_up_to_date(ctx, sec, project, name, behind_is_warning=False)
    check_env_target(ctx, sec, project)

    workflow = check_workflow(ctx, sec, project)
    jobs = jobs_for_host(workflow, ctx.host) if workflow else []
    if workflow and ctx.system == "linux" and jobs:
        rules, _unsupported, problem = ctx.sudoers()
        calls = [call for job in jobs for call in job_sudo_calls(workflow, job.job, ctx.host)]
        if problem:
            sec.warn("sudoers allows this project's deploy", f"could not check ({problem})", "run again after: sudo -v")
        elif calls:
            report_sudo_calls(ctx, sec, "sudoers allows this project's deploy", calls, rules)
    pyproject = load_pyproject(project)
    if ctx.system == "linux":
        check_services(ctx, sec, project, pyproject=pyproject, workflow=workflow, jobs=jobs)
    else:
        check_user_agent(ctx, sec, project, pyproject=pyproject, workflow=workflow, jobs=jobs)
    check_auto_deploy(ctx, sec, project, workflow)
    check_github_environment(ctx, sec, project)


def check_env_target(ctx: Context, sec: Section, project: Path) -> None:
    """Check ``.env.target`` links to a production template, and that its config file exists.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
    """
    templates = sorted(p.name for p in project.glob(".env*.template"))
    if not templates:
        return
    prod = [t for t in templates if ".prod" in t]
    target = project / ".env.target"
    title = ".env.target links to a production template"
    fix = f"cd {ctx.t(project)} && ln -sfn {prod[0] if len(prod) == 1 else '.env.prod.<name>.template'} .env.target"
    if not target.is_symlink():
        if target.is_file():
            sec.warn(title, ".env.target is a copy, not a symlink, so template changes do not reach it", fix)
        else:
            sec.fail(title, f".env.target is missing (templates: {', '.join(templates)})", fix)
            return
    elif not target.exists():
        sec.fail(title, f".env.target points to {target.readlink()}, which does not exist", fix)
        return
    else:
        dest = target.resolve()
        if dest.parent != project.resolve() or dest.name not in templates:
            sec.warn(title, f".env.target points to {target.readlink()}, not a template in the project", fix)
        elif ".dev" in dest.name:
            sec.fail(title, f".env.target points to the development template {dest.name}", fix)
        else:
            sec.ok(title, f".env.target → {dest.name}")
    check_app_config(ctx, sec, project, target)


def check_app_config(ctx: Context, sec: Section, project: Path, target: Path) -> None:
    """Check the config file named by ``APP_CONFIG`` in ``.env.target`` exists.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
        target: The ``.env.target`` file.
    """
    try:
        values = parse_env_file(target.read_text(encoding="utf-8"))
    except OSError:
        return
    config = values.get("APP_CONFIG", "")
    if not config or "op://" in config or "$" in expand_home(config, ctx.home):
        if (project / "configs").is_dir():
            sec.skip("Config file exists", "APP_CONFIG is not set in .env.target, so the config file used is not known")
        return
    path = Path(expand_home(config, ctx.home))
    if not path.is_absolute():
        path = project / path
    if path.is_file():
        sec.ok("Config file exists", ctx.t(path))
    else:
        sec.fail("Config file exists", f"APP_CONFIG is {config}, which does not exist", "create it as the project's README describes")


def check_workflow(ctx: Context, sec: Section, project: Path) -> dict[str, Any] | None:
    """Check the project's deploy.yml has an option and a job for this host.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.

    Returns:
        The parsed workflow, or None if it is missing or invalid.
    """
    path = project / WORKFLOW
    workflow, problem = load_yaml(path)
    title = f"deploy.yml deploys to {ctx.host}"
    if not isinstance(workflow, dict):
        sec.fail(title, f"{WORKFLOW} {problem or 'is not a mapping'}", f"add a deploy workflow: {DOCS_URL}workflow/")
        return None
    options = environment_options(workflow)
    jobs = jobs_for_host(workflow, ctx.host)
    problems = []
    if options is None:
        problems.append("there is no workflow_dispatch 'environment' choice input")
    elif ctx.host not in options:
        near = [o for o in options if o.lower() == ctx.host.lower()]
        hint = f" ('{near[0]}' differs only in case)" if near else ""
        problems.append(f"'{ctx.host}' is not an environment option{hint}; options: {', '.join(options)}")
    if not jobs:
        near = runs_on_case_mismatch(workflow, ctx.host)
        hint = f" (runs-on has '{near[0]}', which differs in case)" if near else ""
        problems.append(f"no job has runs-on: [self-hosted, {ctx.host}]{hint}")
    for job in jobs:
        env = job_environment(job.job, ctx.host)
        if env is None:
            problems.append(f"job {job.job_id} has no 'environment: {ctx.host}'")
        elif env != ctx.host:
            problems.append(f"job {job.job_id} uses environment '{env}', expected '{ctx.host}'")
    if problems:
        sec.fail(title, problems, f"add {ctx.host} to {WORKFLOW}: {DOCS_URL}workflow/")
    else:
        sec.ok(title, ", ".join(f"job {j.job_id}" + (" (matrix)" if j.via == "matrix" else "") for j in jobs))
    return workflow


def _job_calls(workflow: dict[str, Any] | None, jobs: list[HostJob], host: str, script: str) -> bool:
    """Check whether any of the host's jobs run a script.

    Args:
        workflow: Parsed workflow.
        jobs: The host's jobs.
        host: Host name.
        script: Script file name.

    Returns:
        True if a job runs it.
    """
    return workflow is not None and any(job_uses_script(workflow, j.job, host, script) for j in jobs)


def check_services(ctx: Context, sec: Section, project: Path, *, pyproject: dict[str, Any], workflow: dict[str, Any] | None, jobs: list[HostJob]) -> None:
    """Check systemd service templates for a Linux service project.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
        pyproject: Parsed pyproject.toml.
        workflow: Parsed deploy.yml.
        jobs: The host's jobs in it.
    """
    services = project_names(pyproject, "service_name")
    deploys = _job_calls(workflow, jobs, ctx.host, "servicectrl.sh")
    if not services:
        if deploys:
            sec.fail("service_name is set in pyproject.toml", "deploy.yml runs servicectrl.sh, but [project] has no service_name", f'add service_name = "{project.name}" under [project]')
        else:
            sec.skip("Service files", "not a service project (no service_name in pyproject.toml)")
        return
    if not deploys:
        sec.warn("deploy.yml deploys the service", f"service_name is {', '.join(services)}, but this host's job does not run servicectrl.sh")
    for svc in services:
        template = project / "deploy" / ctx.host / f"{svc}.service"
        if not template.is_file():
            sec.fail(f"Service template for {svc}", f"deploy/{ctx.host}/{svc}.service is missing", f"add deploy/{ctx.host}/{svc}.service to the repo")
            continue
        text = template.read_text(encoding="utf-8", errors="replace")
        missing = [f"{key}={value} does not exist" for key, value in _unit_paths(text) if not Path(value).exists()]
        if missing:
            sec.fail(f"Service template for {svc}", missing)
        else:
            sec.ok(f"Service template for {svc}", f"deploy/{ctx.host}/{svc}.service")
        installed = Path("/etc/systemd/system") / f"{svc}.service"
        title = f"Service {svc} is installed and running"
        if not installed.is_file():
            sec.warn(title, f"{installed} is missing; the next deploy installs it")
            continue
        state = run(["systemctl", "is-active", svc]).stdout.strip()
        notes = []
        if installed.read_text(encoding="utf-8", errors="replace") != text:
            notes.append(f"{installed} differs from the template; the next deploy replaces it")
        if state != "active":
            notes.append(f"the service is {state or 'unknown'}")
        if notes:
            sec.warn(title, notes, f"sudo journalctl -u {svc} -n 50" if state != "active" else "")
        else:
            sec.ok(title)


def _unit_paths(text: str) -> list[tuple[str, str]]:
    """Find the files a systemd unit needs: ExecStart, WorkingDirectory and EnvironmentFile.

    Args:
        text: Unit file contents.

    Returns:
        ``(key, path)`` pairs. Optional files (``EnvironmentFile=-...``) are left out.
    """
    found: list[tuple[str, str]] = []
    for raw in text.splitlines():
        key, sep, value = raw.strip().partition("=")
        value = value.strip()
        if not sep or key not in {"ExecStart", "WorkingDirectory", "EnvironmentFile"} or value.startswith("-"):
            continue
        if key == "ExecStart":
            try:
                value = shlex.split(value.lstrip("@+!:"))[0]
            except (ValueError, IndexError):
                continue
        if value.startswith("/"):
            found.append((key, value))
    return found


def check_user_agent(ctx: Context, sec: Section, project: Path, *, pyproject: dict[str, Any], workflow: dict[str, Any] | None, jobs: list[HostJob]) -> None:
    """Check the LaunchAgent plist for a macOS agent project.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
        pyproject: Parsed pyproject.toml.
        workflow: Parsed deploy.yml.
        jobs: The host's jobs in it.
    """
    names = project_names(pyproject, "user_agent_name")
    deploys = _job_calls(workflow, jobs, ctx.host, "useragentctrl.sh")
    if not names:
        if deploys:
            sec.fail("user_agent_name is set in pyproject.toml", "deploy.yml runs useragentctrl.sh, but [project] has no user_agent_name", 'add user_agent_name = "com.example.<project>" under [project]')
        else:
            sec.skip("User agent", "not an agent project (no user_agent_name in pyproject.toml)")
        return
    label = names[0]
    if not deploys:
        sec.skip("User agent", f"user_agent_name is {label}, but this host's job does not run useragentctrl.sh, so no agent is deployed on {ctx.host}")
        return
    template = project / "deploy" / f"{label}.plist"
    title = f"User agent plist for {label}"
    if not template.is_file():
        sec.fail(title, f"deploy/{label}.plist is missing", f"add deploy/{label}.plist to the repo")
        return
    try:
        plist = plistlib.loads(template.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        sec.fail(title, f"deploy/{label}.plist is not a valid plist: {exc}")
        return
    problems = []
    if not isinstance(plist, dict) or plist.get("Label") != label:
        problems.append(f"its Label is {plist.get('Label') if isinstance(plist, dict) else '?'}, expected {label}")
    args = plist.get("ProgramArguments", []) if isinstance(plist, dict) else []
    problems.extend(f"{a} does not exist" for a in args if isinstance(a, str) and a.startswith("/") and not Path(a).exists())
    if problems:
        sec.fail(title, problems)
    else:
        sec.ok(title, f"deploy/{label}.plist")
    installed = ctx.home / "Library/LaunchAgents" / f"{label}.plist"
    if not installed.is_file():
        sec.warn(f"User agent {label} is installed", f"{ctx.t(installed)} is missing; the next deploy installs it")
    elif installed.read_bytes() != template.read_bytes():
        sec.warn(f"User agent {label} is installed", f"{ctx.t(installed)} differs from the template; the next deploy replaces it")
    else:
        sec.ok(f"User agent {label} is installed")


def check_auto_deploy(ctx: Context, sec: Section, project: Path, workflow: dict[str, Any] | None) -> None:
    """Check ``deploy/auto-deploy.yaml``, if the project has one.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
        workflow: Parsed deploy.yml.
    """
    path = project / AUTO_DEPLOY
    title = f"{AUTO_DEPLOY} is valid"
    if not path.exists():
        sec.skip(title, "no auto-deploy control file; this project is deployed by hand")
        return
    data, problem = load_yaml(path)
    if problem:
        sec.fail(title, f"{AUTO_DEPLOY} {problem}")
        return
    review = review_auto_deploy(data, environment_options(workflow) if workflow else None, ctx.host)
    fix = f"{DOCS_URL}auto-deploy/"
    if review.errors:
        sec.fail(title, review.errors + review.warnings, fix)
    elif review.warnings:
        sec.warn(title, review.warnings, fix)
    else:
        sec.ok(title, review.summary)


def check_github_environment(ctx: Context, sec: Section, project: Path) -> None:
    """Check the repo has a GitHub Environment named after this host.

    Args:
        ctx: Where the checks run.
        sec: Section to add results to.
        project: The project's clone.
    """
    title = f"GitHub Environment {ctx.host} exists"
    if not ctx.online:
        sec.skip(title, "--offline")
        return
    if which("gh") is None:
        sec.skip(title, "gh is not installed")
        return
    remote = git(project, "remote", "get-url", "origin")
    parsed = parse_github_remote(remote.stdout) if remote.ok else None
    repo = f"{parsed[0]}/{parsed[1]}" if parsed else f"{ORG}/{project.name}"
    result = run(["gh", "api", f"repos/{repo}/environments", "--jq", ".environments[].name"], timeout=30)
    if not result.ok:
        reason = (result.stderr.strip().splitlines() or ["unknown error"])[0]
        sec.skip(title, f"gh could not list environments: {reason}")
        return
    names = result.stdout.split()
    if ctx.host.lower() in {n.lower() for n in names}:
        sec.ok(title)
    else:
        sec.warn(title, "GitHub creates it on the first deploy, without protection rules", f"https://github.com/{repo}/settings/environments")


# --------------------------------------------------------------------------
# Output and entry point
# --------------------------------------------------------------------------
COLORS = {Status.PASS: "\033[32m", Status.WARN: "\033[33m", Status.FAIL: "\033[31m", Status.SKIP: "\033[90m"}
RESET = "\033[0m"
BOLD = "\033[1m"


def render(sections: list[Section], *, color: bool) -> str:
    """Format the results for the terminal.

    Args:
        sections: Sections to print.
        color: Whether to use ANSI colours.

    Returns:
        The report text, ending with a summary line.
    """
    out: list[str] = []
    counts = dict.fromkeys(Status, 0)
    for section in sections:
        if not section.results:
            continue
        out.append(f"{BOLD}{section.title}{RESET}" if color else section.title)
        for result in section.results:
            counts[result.status] += 1
            label = f"{COLORS[result.status]}{result.status.value}{RESET}" if color else result.status.value
            out.append(f"  {label}  {result.title}")
            out.extend(f"        {line}" for line in result.detail)
            if result.fix:
                out.append(f"        Fix: {result.fix[0]}")
                out.extend(f"             {line}" for line in result.fix[1:])
        out.append("")
    out.append(f"Summary: {counts[Status.PASS]} passed, {counts[Status.WARN]} warnings, {counts[Status.FAIL]} failed, {counts[Status.SKIP]} skipped")
    return "\n".join(out)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments, or None for ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        prog="deploy_check.sh",
        description="Check that this host, and the project in the current folder, are set up for auto-deployment. Read-only.",
        epilog=f"Checks follow {DOCS_URL}. Exit status: 0 if nothing failed, 1 otherwise.",
    )
    parser.add_argument("--project", type=Path, metavar="DIR", help="project folder to check (default: the current folder)")
    parser.add_argument("--host", help="host name / runner label (default: the runner's name, else hostname -s)")
    parser.add_argument("--host-only", action="store_true", help="only check the host, not a project")
    parser.add_argument("--offline", action="store_true", help="skip checks that use the network (git ls-remote, gh)")
    parser.add_argument("--no-sudo", action="store_true", help="never use sudo (root-only checks become warnings)")
    parser.add_argument("--no-color", action="store_true", help="plain output")
    return parser.parse_args(argv)


def deploy_user() -> tuple[str, int, Path]:
    """Work out the deploy user (the one running this, or the one who ran sudo).

    Returns:
        ``(name, uid, home)``.
    """
    name = os.environ.get("SUDO_USER") if os.geteuid() == 0 else None
    name = name or getpass.getuser()
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return name, os.getuid(), Path.home()
    home = Path.home() if entry.pw_uid == os.getuid() else Path(entry.pw_dir)
    return name, entry.pw_uid, home


def find_project(args: argparse.Namespace) -> Path | None:
    """Find the project folder to check.

    Args:
        args: Parsed options.

    Returns:
        The repository root, or None for host checks only.
    """
    if args.host_only:
        return None
    start = (args.project or Path.cwd()).expanduser().resolve()
    top = git(start, "rev-parse", "--show-toplevel")
    if not top.ok:
        return None
    return Path(top.stdout.strip()).resolve()


def prime_sudo(ctx: Context) -> None:
    """On Linux, ask for the sudo password once (if needed) so ``sudo -n`` works.

    Only runs on a terminal. Nothing is changed; this just caches sudo's
    credentials, the same as ``sudo -v``.

    Args:
        ctx: Where the checks run.
    """
    if ctx.system != "linux" or not ctx.use_sudo or os.geteuid() == 0 or not sys.stdin.isatty():
        return
    if run(["sudo", "-n", "true"]).ok:
        return
    print("Some checks read root-only files. Enter your sudo password, or press Ctrl-C and use --no-sudo.", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt, OSError):
        subprocess.run(["sudo", "-v"], check=False)  # noqa: S607


def resolve_host(given: str | None, runner: dict[str, Any] | None) -> tuple[str, str]:
    """Work out the host name: ``--host``, else the runner's name, else ``hostname -s``.

    Args:
        given: Value of ``--host``, if any.
        runner: Parsed ``.runner`` file, if it could be read.

    Returns:
        ``(host, where it came from)``.
    """
    if given:
        return given, "--host"
    if runner and runner.get("agentName"):
        return str(runner["agentName"]), "runner"
    return socket.gethostname().split(".", 1)[0], "hostname -s"


def run_checks(ctx: Context, runner: dict[str, Any] | None, runner_problem: str, *, host_only: bool) -> list[Section]:
    """Run every check that applies here.

    Args:
        ctx: Where the checks run.
        runner: Parsed ``.runner`` file, if it could be read.
        runner_problem: Why it could not be read.
        host_only: Skip the project checks.

    Returns:
        The sections, in report order.
    """
    platform_name = "Linux" if ctx.system == "linux" else "macOS"
    host_sec = Section(f"Host {ctx.host} ({platform_name}, user {ctx.user}, host name from {ctx.host_source})")
    check_host_common(ctx, host_sec, runner, runner_problem)
    check_runner_online(ctx, host_sec)
    platform_sec = Section(f"Host: {platform_name} setup")
    if ctx.system == "linux":
        check_host_linux(ctx, platform_sec)
    else:
        check_host_macos(ctx, platform_sec)
    sections = [host_sec, platform_sec]
    if ctx.project is not None:
        project_sec = Section(f"Project {ctx.project.name} ({ctx.t(ctx.project)})")
        check_project(ctx, project_sec, ctx.project)
        sections.append(project_sec)
    elif not host_only:
        project_sec = Section("Project")
        project_sec.skip("Project checks", f"{Path.cwd()} is not in a git clone; run this from a project folder, e.g. cd ~/scripts/<Project>")
        sections.append(project_sec)
    return sections


def main(argv: list[str] | None = None) -> int:
    """Run the checks and print the report.

    Args:
        argv: Arguments, or None for ``sys.argv``.

    Returns:
        0 if nothing failed, else 1.
    """
    args = parse_args(argv)
    system = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
    if system is None:
        print(f"deploy_check: unsupported platform {platform.system()}", file=sys.stderr)
        return 2
    user, uid, home = deploy_user()
    runner_dir, runner_dir_source = find_runner_dir(system, home, args.host)
    ctx = Context(
        system=system,
        host="",
        host_source="",
        user=user,
        uid=uid,
        home=home,
        project=None,
        online=not args.offline,
        use_sudo=not args.no_sudo,
        script_dir=Path(__file__).resolve().parent,
        runner_dir=runner_dir,
        runner_dir_source=runner_dir_source,
    )
    prime_sudo(ctx)
    runner, runner_problem = read_runner_config(runner_dir, use_sudo=ctx.use_sudo)
    ctx.host, ctx.host_source = resolve_host(args.host, runner)
    ctx.project = find_project(args)
    sections = run_checks(ctx, runner, runner_problem, host_only=args.host_only)
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    print(render(sections, color=color))
    failed = any(r.status is Status.FAIL for s in sections for r in s.results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
