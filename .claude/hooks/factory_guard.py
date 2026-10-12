#!/usr/bin/env python3
"""PreToolUse guard for the Software Factory.

Runs before Bash, Edit, Write, MultiEdit, NotebookEdit, Read and Grep. It is a
no-op unless at least one factory job is active (not merged or abandoned), so
normal Claude Code sessions in the repo are unaffected.

While a job is active, for every caller:
  - the file tools cannot write to the main checkout outside factory/jobs/;
  - the factory's own guardrails (.claude/, factory/bin/, factory/config.json,
    factory/dashboard/) cannot be edited, in the main checkout or a worktree;
  - secret env files and credential stores cannot be read or written;
  - shell commands cannot push, change remotes, merge, rebase, reset, move refs,
    delete branches or worktrees, change git config, use sudo or pipe downloads
    into a shell; git commands that change a working tree must run inside a
    job worktree.

Additionally, for subagents (tool calls whose hook input carries agent_id):
  - nothing in factory/jobs/ can be written: agents return their documents and
    the orchestrator saves them. The only exception is screenshots under
    factory/jobs/<job>/round-<N>/screens/;
  - the control script can only be used for `port`.

This is defense in depth, not a sandbox: shell commands are checked with
patterns, and a determined process can find constructs they miss. Run untrusted
work in a container.

Exit code 2 blocks the tool call and shows the reason to Claude.
Set FACTORY_GUARD=off in the environment that launches Claude to disable it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PROTECTED = (".claude/", "factory/bin/", "factory/config.json", "factory/dashboard/")
HOME = Path.home()
CREDENTIAL_DIRS = [HOME / d for d in (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config/gcloud", ".azure")]
CREDENTIAL_FILES = {".npmrc", ".pypirc", ".netrc", ".git-credentials"}
SAFE_ENV_SUFFIXES = ("example", "sample", "template")
TERMINAL = {"merged", "abandoned"}

# git global options (-c k=v, -C dir, --git-dir=…, --no-pager …) are stripped before matching.
GIT_GLOBALS = re.compile(r"\bgit((?:\s+(?:-[cC]\s+\S+|--[\w-]+(?:=\S+)?|-[pP]))+)(?=\s)")

BASH_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b"), "the factory never pushes; a human pushes after merge"),
    (re.compile(r"\bgit\s+remote\s+(add|set-url|remove|rm|rename|set-head)\b"), "remotes are off-limits"),
    (re.compile(r"\bgit\s+reset\s+--(hard|merge|keep)\b"), "hard resets are off-limits; make a new commit instead"),
    (re.compile(r"\bgit\s+clean\b"), "git clean is off-limits"),
    (re.compile(r"\bgit\s+(merge|rebase|cherry-pick|revert|am|pull)(?![\w-])"), "merges go through 'factory.py merge'"),
    (re.compile(r"\bgit\s+(update-ref|symbolic-ref|replace|filter-branch|filter-repo)\b"), "moving refs directly is off-limits"),
    (re.compile(r"\bgit\s+branch\b[^\n;&|]*\s(-[a-zA-Z]*[dDmMcCf]|--delete|--move|--copy|--force)\b"), "branches are managed by factory.py"),
    (re.compile(r"\bgit\s+worktree\s+(add|remove|prune|move|repair)\b"), "worktrees are managed by factory.py"),
    (re.compile(r"\bgit\s+config\b(?![^\n;&|]*\s(--get|--get-all|--get-regexp|--list|-l)\b)"), "changing git config is off-limits during a factory run"),
    (re.compile(r"\bgit\s+commit\b[^\n]*(--no-verify|\s-n\b)"), "do not skip commit hooks"),
    (re.compile(r"\bsudo\b|\bdoas\b"), "sudo is off-limits"),
    (re.compile(r"\b(curl|wget)\b[^|;&\n]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b"), "piping downloads into a shell is off-limits"),
    (re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\$HOME|\.\.?)/?(\s|$)"), "recursive delete of a root, home or parent directory"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\b"), "world-writable permissions are off-limits"),
    (re.compile(r"(^|[\s'\"=/:<>])\.env(\.(?!(?:example|sample|template)\b)[\w.-]+)?(?=$|[\s'\";|&)<>])"), "secret env files are off-limits"),
    (re.compile(r"\.ssh/|\bid_(rsa|ed25519|ecdsa)\b|\.aws/|\.gnupg/|\.kube/|\.docker/config|\.npmrc|\.pypirc|\.netrc|\.git-credentials"),
     "credential stores are off-limits"),
    (re.compile(r"\bFACTORY_GUARD\b"), "the guard cannot be configured from inside a run"),
]
# git subcommands that change a working tree or the index; they must run inside a job worktree.
TREE_CHANGING_GIT = re.compile(r"\bgit\s+(add|commit|checkout|switch|restore|rm|mv|stash|apply|reset|tag)\b")
WRITE_INTENT = re.compile(
    r"(?<![0-9&])>(?!&)|\btee\b|\bsed\s+(-\w*\s+)*-i|\bperl\s+-\w*i|\bmv\b|\bcp\b|\brm\b|\bchmod\b|\bchown\b"
    r"|\btruncate\b|\bln\b|\binstall\s|\bdd\b|\btouch\b|\brsync\b|\bwrite_text\b|\bopen\([^)]*['\"][wa]"
)
# The one control-script call subagents may make, as a whole command segment.
ALLOWED_AGENT_CLI = re.compile(
    r"(?:^|(?<=[;&|])|(?<=&&))\s*(?:python3?(?:\.\d+)?\s+)?['\"]?/?(?:[\w.-]+/)*factory/bin/factory\.py['\"]?\s+port\s*(?=$|[;&|])"
)
# The control script being executed: `python3 …/factory/bin/factory.py` or `…/factory/bin/factory.py` as a command.
FACTORY_CLI = re.compile(
    r"(?:\b(?:python3?(?:\.\d+)?|py)\s+(?:-\w+\s+)*|(?:^|(?<=[;&|(])\s*|(?<=&&)\s*))['\"]?(?:\S*/)?factory/bin/factory\.py\b['\"]?"
)


def project_root(data: dict) -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    return Path(raw).resolve()


def active(root: Path) -> bool:
    jobs = root / "factory" / "jobs"
    if not jobs.is_dir():
        return False
    for path in jobs.glob("*/job.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("stage") not in TERMINAL:
                return True
        except (OSError, ValueError):
            return True  # unreadable state: stay guarded
    return False


def worktree_root(root: Path) -> Path:
    template = "../{repo}-factory"
    try:
        template = json.loads((root / "factory" / "config.json").read_text(encoding="utf-8")).get("worktree_root", template)
    except (OSError, ValueError):
        pass
    raw = Path(template.format(repo=root.name))
    return (raw if raw.is_absolute() else root / raw).resolve()


def is_secret_file(path: Path) -> bool:
    name = path.name
    if name == ".env" or (name.startswith(".env.") and name.rsplit(".", 1)[-1] not in SAFE_ENV_SUFFIXES):
        return True
    if name in CREDENTIAL_FILES:
        return True
    return any(d == path or d in path.parents for d in CREDENTIAL_DIRS)


def inside(path: Path, base: Path) -> bool:
    return base == path or base in path.parents


def check_path(raw: str, tool: str, root: Path, data: dict, subagent: bool) -> str | None:
    cwd = Path(data.get("cwd") or root)
    path = (Path(raw) if Path(raw).is_absolute() else cwd / raw).resolve()
    if is_secret_file(path):
        return f"{path.name} is a secret or credential file"
    if tool in ("Read", "Grep"):
        return None
    wt_root = worktree_root(root)
    for base in [root, *(p for p in (wt_root.iterdir() if wt_root.is_dir() else []) if p.is_dir())]:
        if inside(path, base):
            rel = path.relative_to(base).as_posix()
            if any(rel == p.rstrip("/") or rel.startswith(p) for p in PROTECTED):
                return f"{rel} is part of the factory's guardrails and is read-only while jobs run"
    if inside(path, root):
        rel = path.relative_to(root).as_posix()
        if not rel.startswith("factory/jobs/"):
            return (f"the main checkout is read-only while factory jobs run ({rel}); "
                    "edit files in the job's worktree instead")
        if subagent and not re.match(r"^factory/jobs/[^/]+/round-\d+/screens/", rel):
            return ("agents do not write job files; return your document as your final message "
                    "and the orchestrator will save it")
    return None


def strip_git_globals(command: str) -> str:
    return GIT_GLOBALS.sub("git", command)


def check_bash(command: str, root: Path, subagent: bool) -> str | None:
    normalized = strip_git_globals(command)
    for pattern, reason in BASH_RULES:
        if pattern.search(normalized):
            return reason

    if subagent:
        leftover = ALLOWED_AGENT_CLI.sub(" ", command)
        if re.search(r"factory\.py|factory/bin|\bimport\s+factory\b|\bfrom\s+factory\b", leftover):
            return "agents may only run 'factory.py port'; the orchestrator owns job state"
        if "factory/jobs/" in command and WRITE_INTENT.search(command) and "/screens/" not in command:
            return "agents do not write job files; return your document as your final message"

    # Invocations of the control script are fine; look for writes in the rest of the command.
    without_cli = FACTORY_CLI.sub(" FX ", command)
    if any(p in without_cli for p in PROTECTED) and WRITE_INTENT.search(without_cli):
        return "the factory's guardrails (.claude/, factory/bin/, factory/config.json, factory/dashboard/) are read-only"

    if TREE_CHANGING_GIT.search(normalized):
        wt = re.escape(str(worktree_root(root)))
        if not re.match(rf"^\s*cd\s+(['\"]?){wt}/[^\s'\"]+\1\s*&&", command):
            return ("git commands that change files must run inside a job worktree: "
                    "start the command with 'cd <worktree> && '")
    return None


def decide(data: dict) -> str | None:
    root = project_root(data)
    if not active(root):
        return None
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    subagent = bool(data.get("agent_id"))
    if tool == "Bash":
        return check_bash(tool_input.get("command", ""), root, subagent)
    if tool == "Grep":
        for key in ("path", "glob"):
            value = tool_input.get(key)
            if value and is_secret_file(Path(value)):
                return f"{Path(value).name} is a secret or credential file"
        return None
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        return check_path(raw, tool, root, data, subagent) if raw else None
    return None


def main() -> int:
    if os.environ.get("FACTORY_GUARD", "on").lower() in ("off", "0", "false", "no"):
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("factory guard: could not parse hook input", file=sys.stderr)
        return 2
    try:
        reason = decide(data)
    except Exception as exc:  # fail closed while a job may be running
        print(f"factory guard: internal error ({exc}); set FACTORY_GUARD=off to bypass", file=sys.stderr)
        return 2
    if reason:
        print(f"factory guard: blocked. {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
