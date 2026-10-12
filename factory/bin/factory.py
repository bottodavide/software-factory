#!/usr/bin/env python3
"""Software Factory control script.

Deterministic plumbing for the /factory loop: job state, git worktrees,
evidence packs, quality gates, risk classification, guarded merges and the
local dashboard. Standard library only; Python 3.10+, git 2.38+.

The orchestrating Claude session calls this script instead of running raw git
commands, so every state change and every merge goes through the same checks.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import fnmatch
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STAGES = ("spec", "build", "review", "approve", "merged", "needs-human", "abandoned")
TERMINAL_STAGES = frozenset({"merged", "abandoned"})
VERDICTS = ("PASS", "CHANGES")
JOB_ID_RE = re.compile(r"^\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*$")
VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|CHANGES)\s*$")

DEFAULT_CONFIG: dict = {
    "main_branch": "main",
    "worktree_root": "../{repo}-factory",
    "max_rework_rounds": 2,
    "dev_host": "127.0.0.1",
    "reviewers": ["security", "code", "experience"],
    "gates": {
        "commands": [],
        "timeout_seconds": 900,
        "sast_config": "p/default",
    },
    "risk": {
        "max_changed_files": 20,
        "max_changed_lines": 500,
        "test_globs": ["*test*", "*spec.*", "*/tests/*", "tests/*", "*/__tests__/*"],
        "rules": {
            "identity-and-access": [
                "auth/*", "*/auth/*", "*/authentication/*", "*/authorization/*",
                "*login*", "*oauth*", "*permission*", "*rbac*", "*session*",
                "*jwt*", "*password*",
            ],
            "payments": [
                "*payment*", "*billing*", "*invoice*", "*checkout*", "*stripe*",
                "*paypal*", "*subscription*",
            ],
            "ci-deploy-infra": [
                ".github/*", ".gitlab-ci.yml", ".circleci/*", "Jenkinsfile",
                "*Dockerfile*", "*docker-compose*", "*compose.yml", "*compose.yaml",
                "*.tf", "*.tfvars", "k8s/*", "*/k8s/*", "helm/*", "*/helm/*",
                "deploy/*", "*deploy*.sh", "Procfile", "Makefile", "vercel.json",
                "netlify.toml", "firebase.json", "*.rules", "*nginx*.conf",
            ],
            "dependencies": [
                "package.json", "*/package.json", "package-lock.json",
                "*/package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
                "yarn.lock", ".npmrc", "requirements*.txt", "*/requirements*.txt",
                "pyproject.toml", "*/pyproject.toml", "poetry.lock", "uv.lock",
                "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg", ".pypirc",
            ],
            "database": [
                "migrations/*", "*/migrations/*", "alembic/*", "*/alembic/*",
                "*.sql", "*schema.prisma", "*/prisma/*",
            ],
            "secrets-and-config": [
                ".env", ".env.*", "*/.env", "*/.env.*", "*secret*", "*.pem",
                "*.key", "*.p12", "*.pfx", "*credentials*",
            ],
            "factory-and-agent-instructions": [
                ".claude/*", "factory/bin/*", "factory/config.json",
                "factory/dashboard/*", "AGENTS.md", "CLAUDE.md", "*/AGENTS.md",
                "*/CLAUDE.md",
            ],
        },
    },
}

STOPWORDS = frozenset(
    "a an the to of for and or on in with add make show when into from by as at "
    "is be that this it new instead please should can".split()
)

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("cloud access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("code host token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}|glpat-[A-Za-z0-9_\-]{20,})")),
    ("chat token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("browser api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("live payment key", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{20,}")),
    ("model api key", re.compile(r"\bsk-[A-Za-z0-9_\-]{32,}")),
    ("hard-coded credential", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\b\s*[:=]\s*['\"]([^'\"\s]{12,})['\"]")),
]
PLACEHOLDER_HINTS = ("example", "xxxx", "changeme", "placeholder", "dummy", "<", "${", "your_", "your-", "redacted")


class FactoryError(Exception):
    """A precondition failed. The message is meant for the orchestrator."""


class NeedsHuman(FactoryError):
    """The job cannot proceed automatically and was moved to needs-human."""


# --------------------------------------------------------------------------- utils

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise FactoryError(f"git {' '.join(args)} failed: {detail}")
    return proc


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: object) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def slugify(feature: str) -> str:
    words = re.findall(r"[a-z0-9]+", feature.lower())
    keep = [w for w in words if w not in STOPWORDS] or words
    slug = "-".join(keep[:4])[:40].strip("-")
    return slug or "job"


def main_checkout(start: Path | None = None) -> Path:
    """The main working tree, even when called from inside a job worktree."""
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start or Path.cwd())
    return Path(common.stdout.strip()).parent.resolve()


def first_line(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as fh:
            return fh.readline().strip()
    except FileNotFoundError:
        return ""


def matches(path: str, patterns: list[str]) -> bool:
    lowered = path.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


# ------------------------------------------------------------------------- factory

class Factory:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.dir = self.root / "factory"
        self.jobs = self.dir / "jobs"
        cfg_path = self.dir / "config.json"
        user_cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        self.cfg = deep_merge(DEFAULT_CONFIG, user_cfg)
        self.main = self.cfg["main_branch"]
        self.reviewers: list[str] = list(self.cfg["reviewers"])
        wt_root = self.worktree_root()
        if wt_root == self.root or self.root in wt_root.parents:
            raise FactoryError("worktree_root must be outside the repository")

    # ---- paths and state

    def worktree_root(self) -> Path:
        raw = Path(self.cfg["worktree_root"].format(repo=self.root.name))
        return (raw if raw.is_absolute() else self.root / raw).resolve()

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise FactoryError(f"invalid job id: {job_id!r}")
        return self.jobs / job_id

    def load(self, job_id: str) -> dict:
        path = self.job_dir(job_id) / "job.json"
        if not path.exists():
            raise FactoryError(f"no such job: {job_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, job: dict) -> None:
        job["updated_at"] = now()
        write_json(self.job_dir(job["id"]) / "job.json", job)

    def set_stage(self, job: dict, stage: str, note: str | None = None) -> None:
        if stage not in STAGES:
            raise FactoryError(f"invalid stage {stage!r}; expected one of {', '.join(STAGES)}")
        if job.get("stage") != stage:
            job.setdefault("history", []).append({"at": now(), "stage": stage, **({"note": note} if note else {})})
        job["stage"] = stage
        if note:
            job["note"] = note

    def active_jobs(self) -> list[dict]:
        out = []
        if self.jobs.is_dir():
            for path in sorted(self.jobs.glob("*/job.json")):
                with contextlib.suppress(ValueError, OSError):
                    job = json.loads(path.read_text(encoding="utf-8"))
                    if job.get("stage") not in TERMINAL_STAGES:
                        out.append(job)
        return out

    # ---- job creation

    def _claim_number(self) -> int:
        claims = self.jobs / ".claims"
        claims.mkdir(parents=True, exist_ok=True)
        taken = [int(p.name[:3]) for p in self.jobs.iterdir() if re.match(r"^\d{3}-", p.name)]
        taken += [int(p.name) for p in claims.iterdir() if p.name.isdigit()]
        n = max(taken, default=0) + 1
        while n <= 999:
            try:
                (claims / f"{n:03d}").mkdir()
                return n
            except FileExistsError:
                n += 1
        raise FactoryError("job numbers exhausted (999)")

    def preflight(self) -> None:
        git("rev-parse", "--verify", "--quiet", f"refs/heads/{self.main}", cwd=self.root)
        probe = git("check-ignore", "-q", "factory/jobs/probe", cwd=self.root, check=False)
        if probe.returncode != 0:
            raise FactoryError("factory/jobs/ is not gitignored; add '/factory/jobs/' to .gitignore and commit it")

    def init_job(self, feature: str) -> dict:
        feature = " ".join(feature.split())
        if not feature:
            raise FactoryError("feature description is empty")
        self.preflight()
        n = self._claim_number()
        job_id = f"{n:03d}-{slugify(feature)}"
        branch = f"factory/{job_id}"
        worktree = self.worktree_root() / job_id
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            git("worktree", "add", "-b", branch, str(worktree), self.main, cwd=self.root)
        except FactoryError:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        job = {
            "id": job_id,
            "feature": feature,
            "branch": branch,
            "worktree": str(worktree),
            "base": self.main,
            "stage": "spec",
            "round": 0,
            "reviews": {r: "pending" for r in self.reviewers},
            "gates": "pending",
            "risk": [],
            "rework_base": 0,
            "created_at": now(),
            "history": [{"at": now(), "stage": "spec"}],
        }
        self.save(job)
        return job

    def claim_next(self) -> dict:
        backlog = self.dir / "backlog.md"
        if not backlog.exists():
            raise FactoryError("factory/backlog.md does not exist")
        with self._locked(self.dir / ".backlog.lock"):
            lines = backlog.read_text(encoding="utf-8").splitlines(keepends=True)
            idx = next((i for i, l in enumerate(lines) if re.match(r"^- \[ \] \S", l)), None)
            if idx is None:
                raise FactoryError("backlog has no unchecked items")
            item = lines[idx][len("- [ ] "):].strip()
            lines[idx] = f"- [~] {item} → claiming\n"
            atomic_write(backlog, "".join(lines))
        try:
            job = self.init_job(item)
        except BaseException:
            self._replace_backlog_line(f"- [~] {item} → claiming", f"- [ ] {item}")
            raise
        self._replace_backlog_line(f"- [~] {item} → claiming", f"- [x] {item} → {job['id']}")
        return job

    def _replace_backlog_line(self, old: str, new: str) -> None:
        backlog = self.dir / "backlog.md"
        with self._locked(self.dir / ".backlog.lock"):
            lines = backlog.read_text(encoding="utf-8").splitlines(keepends=True)
            for i, line in enumerate(lines):
                if line.rstrip("\n") == old:
                    lines[i] = new + "\n"
                    break
            atomic_write(backlog, "".join(lines))

    @staticmethod
    @contextlib.contextmanager
    def _locked(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ---- state updates

    def update(self, job_id: str, assignments: list[str]) -> dict:
        job = self.load(job_id)
        for item in assignments:
            key, sep, value = item.partition("=")
            if not sep:
                raise FactoryError(f"expected key=value, got {item!r}")
            if key == "stage":
                if value in TERMINAL_STAGES:
                    raise FactoryError(f"stage {value!r} is set only by 'merge' or 'abandon'")
                self.set_stage(job, value)
            elif key == "rework_base":
                if not value.isdigit():
                    raise FactoryError("rework_base must be a non-negative integer")
                job["rework_base"] = int(value)
            elif key == "round":
                if not value.isdigit():
                    raise FactoryError("round must be a non-negative integer")
                if int(value) != job.get("round"):
                    job["gates"] = "pending"
                job["round"] = int(value)
            elif key == "reviews":
                if value != "pending":
                    raise FactoryError("reviews=<value> only accepts 'pending' (resets every reviewer)")
                job["reviews"] = {r: "pending" for r in self.reviewers}
            elif key.startswith("reviews."):
                name = key.split(".", 1)[1]
                if name not in self.reviewers:
                    raise FactoryError(f"unknown reviewer {name!r}")
                if value not in (*VERDICTS, "pending", "skipped"):
                    raise FactoryError(f"invalid verdict {value!r}")
                job["reviews"][name] = value
            elif key == "note":
                job["note"] = value
            else:
                raise FactoryError(f"unknown key {key!r}")
        self.save(job)
        return job

    # ---- evidence, risk and gates

    def changes(self, branch: str) -> list[dict]:
        rng = f"{self.main}...{branch}"
        numstat = git("diff", "--numstat", "-z", "--no-renames", rng, cwd=self.root).stdout
        status = git("diff", "--name-status", "-z", "--no-renames", rng, cwd=self.root).stdout
        kinds: dict[str, str] = {}
        parts = status.split("\0")
        for i in range(0, len(parts) - 1, 2):
            kinds[parts[i + 1]] = parts[i]
        out = []
        for entry in numstat.split("\0"):
            if not entry:
                continue
            added, deleted, path = entry.split("\t", 2)
            out.append({
                "path": path,
                "status": kinds.get(path, "M"),
                "added": int(added) if added.isdigit() else 0,
                "deleted": int(deleted) if deleted.isdigit() else 0,
            })
        return out

    def classify(self, changes: list[dict]) -> list[dict]:
        risk_cfg = self.cfg["risk"]
        flags: list[dict] = []
        if not changes:
            flags.append({"rule": "empty-change", "detail": "the branch changes nothing"})
        for rule, patterns in risk_cfg["rules"].items():
            hits = sorted({c["path"] for c in changes if matches(c["path"], patterns)})
            if hits:
                flags.append({"rule": rule, "paths": hits[:25]})
        n_files = len(changes)
        n_lines = sum(c["added"] + c["deleted"] for c in changes)
        if n_files > risk_cfg["max_changed_files"]:
            flags.append({"rule": "size", "detail": f"{n_files} files changed (limit {risk_cfg['max_changed_files']})"})
        if n_lines > risk_cfg["max_changed_lines"]:
            flags.append({"rule": "size", "detail": f"{n_lines} lines changed (limit {risk_cfg['max_changed_lines']})"})
        deleted_tests = [c["path"] for c in changes if c["status"].startswith("D") and matches(c["path"], risk_cfg["test_globs"])]
        if deleted_tests:
            flags.append({"rule": "tests-deleted", "paths": deleted_tests})
        return flags

    def evidence(self, job_id: str, round_no: int) -> dict:
        job = self.load(job_id)
        worktree = Path(job["worktree"])
        rdir = self.job_dir(job_id) / f"round-{round_no}"
        rdir.mkdir(parents=True, exist_ok=True)
        branch = job["branch"]
        head = git("rev-parse", branch, cwd=self.root).stdout.strip()
        changes = self.changes(branch)
        atomic_write(rdir / "diff.patch", git("diff", f"{self.main}...{branch}", cwd=self.root).stdout)
        atomic_write(rdir / "commits.txt", git("log", "--oneline", f"{self.main}..{branch}", cwd=self.root).stdout)
        dirty = git("status", "--porcelain", cwd=worktree).stdout.strip()
        gates = run_gates(self, worktree, changes, rdir, dirty)
        risk = self.classify(changes)
        advisory = [g["name"] for g in gates["gates"] if not g["blocking"] and g["status"] == "fail"]
        if advisory:
            risk.append({"rule": "advisory-gate-failed", "detail": ", ".join(advisory)})
        write_json(rdir / "changes.json", {"head": head, "base": self.main, "files": changes})
        write_json(rdir / "risk.json", risk)
        write_json(rdir / "gates.json", gates)
        job["round"] = round_no
        job["head"] = head
        job["gates"] = "CHANGES" if gates["blocking_failed"] else "PASS"
        job["risk"] = [f["rule"] for f in risk]
        self.save(job)
        return {
            "job": job_id,
            "round": round_no,
            "head": head,
            "files_changed": len(changes),
            "lines_changed": sum(c["added"] + c["deleted"] for c in changes),
            "gates": {g["name"]: g["status"] for g in gates["gates"]},
            "blocking_failed": gates["blocking_failed"],
            "risk": [f["rule"] for f in risk],
            "rework_allowed": self.rework_allowed(job, round_no),
            "round_dir": str(rdir),
        }

    def rework_allowed(self, job: dict, round_no: int) -> bool:
        """Whether the builder may be sent back after this round."""
        return round_no - int(job.get("rework_base", 0)) <= int(self.cfg["max_rework_rounds"])

    def check_round(self, job_id: str, round_no: int) -> dict:
        rdir = self.job_dir(job_id) / f"round-{round_no}"
        verdicts: dict[str, str] = {}
        missing, invalid = [], []
        for reviewer in self.reviewers:
            path = rdir / f"review-{reviewer}.md"
            if not path.exists():
                missing.append(reviewer)
                continue
            m = VERDICT_RE.match(first_line(path))
            if not m:
                invalid.append(reviewer)
            else:
                verdicts[reviewer] = m.group(1)
        gates_path = rdir / "gates.json"
        gates_ok = gates_path.exists() and not json.loads(gates_path.read_text())["blocking_failed"]
        complete = not missing and not invalid
        return {
            "round": round_no,
            "rework_allowed": self.rework_allowed(self.load(job_id), round_no),
            "complete": complete,
            "missing": missing,
            "invalid": invalid,
            "verdicts": verdicts,
            "gates": "PASS" if gates_ok else "CHANGES",
            "all_pass": complete and gates_ok and all(v == "PASS" for v in verdicts.values()),
        }

    # ---- merge and cleanup

    def _main_is_clean(self) -> str | None:
        branch = git("branch", "--show-current", cwd=self.root).stdout.strip()
        if branch != self.main:
            return f"the main checkout is on {branch or 'a detached HEAD'}, not {self.main}"
        dirty = git(
            "status", "--porcelain", "--", ".", ":(exclude)factory/jobs", ":(exclude)factory/backlog.md",
            ":(exclude)factory/.backlog.lock", cwd=self.root,
        ).stdout.strip()
        if dirty:
            return "the main checkout has uncommitted changes:\n" + dirty
        return None

    def merge(self, job_id: str, human: bool = False) -> dict:
        job = self.load(job_id)
        if job["stage"] in TERMINAL_STAGES:
            raise FactoryError(f"{job_id} is already {job['stage']}")
        if human and job["stage"] != "needs-human":
            raise FactoryError(f"{job_id} is {job['stage']}; only a job waiting for a human can be approved by one")
        worktree = Path(job["worktree"])
        branch = job["branch"]
        round_no = job.get("round", 0)
        rdir = self.job_dir(job_id) / f"round-{round_no}"

        def refuse(reason: str) -> None:
            self.set_stage(job, "needs-human", note=reason)
            self.save(job)
            raise NeedsHuman(reason)

        if round_no < 1 or not (rdir / "changes.json").exists():
            refuse("no evidence pack for the current round; run evidence first")
        recorded = json.loads((rdir / "changes.json").read_text())["head"]
        current = git("rev-parse", branch, cwd=self.root).stdout.strip()
        if current != recorded:
            refuse(f"branch moved since round {round_no} was reviewed ({recorded[:8]} → {current[:8]})")
        if worktree.exists() and git("status", "--porcelain", cwd=worktree).stdout.strip():
            refuse("the job worktree has uncommitted changes")
        gates_path = rdir / "gates.json"
        if not gates_path.exists() or json.loads(gates_path.read_text())["blocking_failed"]:
            refuse(f"blocking quality gates did not pass in round {round_no}")

        if not human:
            if first_line(self.job_dir(job_id) / "decision.md") != "APPROVE":
                refuse("decision.md does not start with APPROVE")
            state = self.check_round(job_id, round_no)
            if not state["all_pass"]:
                refuse(f"round {round_no} is not all PASS: {state}")
            risk = self.classify(self.changes(branch))
            gates = json.loads((rdir / "gates.json").read_text())
            risk += [{"rule": "advisory-gate-failed"} for g in gates["gates"] if not g["blocking"] and g["status"] == "fail"][:1]
            if risk:
                refuse("risk flags require a human: " + ", ".join(sorted({f["rule"] for f in risk})))

        busy = self._main_is_clean()
        if busy:
            refuse(f"approved, but {busy}. Run /factory approve {job_id} when it is clean")
        probe = git("merge-tree", "--write-tree", self.main, branch, cwd=self.root, check=False)
        if probe.returncode != 0:
            refuse("the branch conflicts with " + self.main)

        message = f"Merge {branch}: {job['feature']}"[:200]
        result = git("merge", "--no-ff", "--no-edit", "-m", message, branch, cwd=self.root, check=False)
        if result.returncode != 0:
            git("merge", "--abort", cwd=self.root, check=False)
            refuse("git merge failed: " + (result.stderr or result.stdout).strip())
        job["merged_commit"] = git("rev-parse", "HEAD", cwd=self.root).stdout.strip()
        job["merged_by"] = "human" if human else "factory"
        warnings = self._cleanup(job, force=False)
        self.set_stage(job, "merged")
        self.save(job)
        return {"job": job_id, "stage": "merged", "commit": job["merged_commit"], "warnings": warnings}

    def _cleanup(self, job: dict, force: bool) -> list[str]:
        warnings = []
        worktree = Path(job["worktree"])
        if worktree.exists():
            args = ["worktree", "remove", *(["--force"] if force else []), str(worktree)]
            res = git(*args, cwd=self.root, check=False)
            if res.returncode != 0:
                warnings.append("worktree not removed: " + res.stderr.strip())
        res = git("branch", "-D" if force else "-d", job["branch"], cwd=self.root, check=False)
        if res.returncode != 0:
            warnings.append("branch not deleted: " + res.stderr.strip())
        return warnings

    def abandon(self, job_id: str) -> dict:
        job = self.load(job_id)
        if job["stage"] == "merged":
            raise FactoryError(f"{job_id} is merged; nothing to abandon")
        warnings = self._cleanup(job, force=True)
        self.set_stage(job, "abandoned")
        self.save(job)
        return {"job": job_id, "stage": "abandoned", "warnings": warnings}


# --------------------------------------------------------------------------- gates

def _has_module(python: str, module: str, cwd: Path) -> bool:
    code = f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"
    return subprocess.run([python, "-c", code], cwd=cwd, capture_output=True).returncode == 0


def detect_gates(fx: Factory, worktree: Path, changes: list[dict]) -> list[dict]:
    explicit = fx.cfg["gates"]["commands"]
    paths = [c["path"] for c in changes]
    existing = [p for p in paths if (worktree / p).is_file()]
    deps_changed = any(matches(p, fx.cfg["risk"]["rules"]["dependencies"]) for p in paths)
    gates: list[dict] = []

    if explicit:
        for g in explicit:
            if "name" not in g or not ("run" in g or "cmd" in g):
                raise FactoryError(f"gate entries need 'name' and 'run' (shell) or 'cmd' (argv): {g}")
            gates.append({"blocking": True, **g})
        return gates

    pkg = worktree / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except ValueError:
            scripts = {}
        pm = "pnpm" if (worktree / "pnpm-lock.yaml").exists() else "yarn" if (worktree / "yarn.lock").exists() else "npm"
        for name in ("lint", "typecheck", "type-check", "test", "build"):
            body = scripts.get(name, "")
            if body and "no test specified" not in body:
                gates.append({"name": f"node-{name}", "cmd": [pm, "run", name], "blocking": True})
        if deps_changed and pm == "npm" and (worktree / "package-lock.json").exists():
            gates.append({"name": "node-audit", "cmd": ["npm", "audit", "--audit-level=high"], "blocking": False})

    py_project = any((worktree / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"))
    changed_py = [p for p in existing if p.endswith(".py")]
    if py_project or changed_py:
        venv = worktree / ".venv" / "bin" / "python"
        python = str(venv) if venv.exists() else "python3"
        if changed_py:
            if shutil.which("ruff"):
                gates.append({"name": "py-ruff", "cmd": ["ruff", "check", *changed_py], "blocking": True})
            elif _has_module(python, "ruff", worktree):
                gates.append({"name": "py-ruff", "cmd": [python, "-m", "ruff", "check", *changed_py], "blocking": True})
        has_tests = (worktree / "tests").is_dir() or any(worktree.glob("**/test_*.py"))
        if has_tests and _has_module(python, "pytest", worktree):
            gates.append({"name": "py-pytest", "cmd": [python, "-m", "pytest", "-q"], "blocking": True, "skip_codes": [5]})
        if deps_changed and shutil.which("pip-audit"):
            gates.append({"name": "py-audit", "cmd": ["pip-audit"], "blocking": False})

    if shutil.which("gitleaks"):
        gates.append({
            "name": "gitleaks",
            "cmd": ["gitleaks", "detect", "--source", ".", "--log-opts", f"{fx.main}..HEAD", "--redact", "--no-banner"],
            "blocking": True,
        })
    code_files = [p for p in existing if re.search(r"\.(m?[jt]sx?|cjs|py|vue|svelte)$", p)]
    if shutil.which("semgrep") and code_files:
        gates.append({
            "name": "semgrep",
            "cmd": ["semgrep", "scan", "--config", fx.cfg["gates"]["sast_config"], "--error", "--metrics=off", "--quiet", *code_files],
            "blocking": True,
        })
    return gates


def scan_secrets(diff: str) -> list[str]:
    findings = []
    current = "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for label, pattern in SECRET_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            value = m.group(m.lastindex or 0)
            if any(h in value.lower() for h in PLACEHOLDER_HINTS) or "process.env" in line or "os.environ" in line:
                continue
            findings.append(f"{current}: {label} ({value[:4]}…)")
    return findings


def run_gates(fx: Factory, worktree: Path, changes: list[dict], rdir: Path, dirty: str) -> dict:
    logs = rdir / "logs"
    logs.mkdir(exist_ok=True)
    timeout = int(fx.cfg["gates"]["timeout_seconds"])
    env = {**os.environ, "CI": "1", "FORCE_COLOR": "0", "NO_COLOR": "1"}
    results: list[dict] = []

    def record(name: str, status: str, blocking: bool, log: str, code: int | None = None, secs: float = 0.0) -> None:
        log_path = logs / f"{re.sub(r'[^a-z0-9-]+', '-', name.lower())}.log"
        atomic_write(log_path, log[-200_000:])
        results.append({
            "name": name, "status": status, "blocking": blocking, "exit_code": code,
            "seconds": round(secs, 1), "log": str(log_path.relative_to(rdir)),
        })

    record("clean-tree", "fail" if dirty else "pass", True, dirty or "worktree clean\n")
    secrets = scan_secrets((rdir / "diff.patch").read_text(encoding="utf-8"))
    record("secrets", "fail" if secrets else "pass", True, "\n".join(secrets) + "\n" if secrets else "no secrets found in added lines\n")

    for gate in detect_gates(fx, worktree, changes):
        argv = gate.get("cmd")
        shell_cmd = gate.get("run")
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv if argv else shell_cmd, shell=argv is None, cwd=worktree, env=env,
                text=True, capture_output=True, timeout=timeout,
            )
        except FileNotFoundError as exc:
            record(gate["name"], "skipped", gate["blocking"], f"tool not found: {exc}\n")
            continue
        except subprocess.TimeoutExpired:
            record(gate["name"], "fail", gate["blocking"], f"timed out after {timeout}s\n", None, time.monotonic() - started)
            continue
        if proc.returncode == 0:
            status = "pass"
        elif proc.returncode in gate.get("skip_codes", []):
            status = "skipped"
        else:
            status = "fail"
        shown = " ".join(argv) if argv else shell_cmd
        record(gate["name"], status, gate["blocking"], f"$ {shown}\n{proc.stdout}\n{proc.stderr}", proc.returncode, time.monotonic() - started)

    return {
        "gates": results,
        "blocking_failed": any(r["blocking"] and r["status"] == "fail" for r in results),
        "advisory_failed": any(not r["blocking"] and r["status"] == "fail" for r in results),
    }


# ----------------------------------------------------------------------- dashboard

SERVABLE = {".md", ".json", ".txt", ".patch", ".log", ".png"}


def job_summary(fx: Factory, job_dir: Path) -> dict | None:
    try:
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rounds = []
    for rdir in sorted(job_dir.glob("round-*"), key=lambda p: int(p.name.split("-")[1]) if p.name.split("-")[1].isdigit() else 0):
        verdicts = {}
        for r in fx.reviewers:
            m = VERDICT_RE.match(first_line(rdir / f"review-{r}.md"))
            verdicts[r] = m.group(1) if m else None
        gates = None
        with contextlib.suppress(OSError, ValueError, KeyError):
            gates = "CHANGES" if json.loads((rdir / "gates.json").read_text())["blocking_failed"] else "PASS"
        rounds.append({"name": rdir.name, "verdicts": verdicts, "gates": gates})
    files = sorted(
        str(p.relative_to(job_dir)) for p in job_dir.rglob("*")
        if p.is_file() and p.suffix in SERVABLE and not p.name.startswith(".")
    )
    job["rounds"] = rounds
    job["files"] = files
    job["decision"] = first_line(job_dir / "decision.md") or None
    return job


def make_handler(fx: Factory, allowed_hosts: set[str]):
    dashboard = fx.dir / "dashboard" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "factory"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: object, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in allowed_hosts:
                return self._send(403, b"forbidden host", "text/plain")
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                if not dashboard.is_file():
                    return self._send(500, b"factory/dashboard/index.html is missing", "text/plain; charset=utf-8")
                return self._send(200, dashboard.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/jobs":
                jobs = []
                if fx.jobs.is_dir():
                    for d in sorted(fx.jobs.iterdir()):
                        if d.is_dir() and JOB_ID_RE.match(d.name):
                            s = job_summary(fx, d)
                            if s:
                                jobs.append(s)
                return self._json({"jobs": jobs, "reviewers": fx.reviewers, "time": now()})
            if url.path == "/api/file":
                q = parse_qs(url.query)
                job_id = (q.get("job") or [""])[0]
                rel = (q.get("path") or [""])[0]
                if not JOB_ID_RE.match(job_id) or not rel:
                    return self._json({"error": "bad request"}, 400)
                base = (fx.jobs / job_id).resolve()
                target = (base / rel).resolve()
                if base not in target.parents or target.suffix not in SERVABLE or not target.is_file():
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(target.name)[0] if target.suffix == ".png" else "text/plain; charset=utf-8"
                return self._send(200, target.read_bytes(), ctype or "application/octet-stream")
            return self._json({"error": "not found"}, 404)

    return Handler


def free_port(host: str) -> int:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def serve(fx: Factory, port: int | None) -> None:
    host = fx.cfg["dev_host"]
    port = port or free_port(host)
    allowed = {host, "localhost", "127.0.0.1", "::1"}
    httpd = ThreadingHTTPServer((host, port), make_handler(fx, allowed))
    shown = f"[{host}]" if ":" in host else host
    print(f"Factory dashboard on http://{shown}:{port}/  (Ctrl-C to stop)", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()


# -------------------------------------------------------------------------- doctor

def doctor(fx: Factory) -> int:
    ok = True

    def line(status: str, text: str) -> None:
        print(f"[{status:>4}] {text}")

    version = git("--version", cwd=fx.root).stdout.split()[-1]
    major, minor = (int(x) for x in re.findall(r"\d+", version)[:2])
    if (major, minor) >= (2, 38):
        line("ok", f"git {version}")
    else:
        ok = False
        line("FAIL", f"git {version}: need 2.38+ for merge-tree --write-tree")
    if sys.version_info >= (3, 10):
        line("ok", f"python {sys.version.split()[0]}")
    else:
        ok = False
        line("FAIL", "python 3.10+ required")
    try:
        fx.preflight()
        line("ok", f"branch {fx.main} exists and factory/jobs/ is gitignored")
    except FactoryError as exc:
        ok = False
        line("FAIL", str(exc))
    agents = next((n for n in ("AGENTS.md", "CLAUDE.md") if (fx.root / n).exists()), None)
    if agents:
        text = (fx.root / agents).read_text(encoding="utf-8").lower()
        missing = [s for s in ("setup", "dev server", "validation") if f"## {s}" not in text]
        line("ok" if not missing else "warn", f"{agents} found" + (f"; missing sections: {', '.join(missing)}" if missing else ""))
    else:
        ok = False
        line("FAIL", "no AGENTS.md or CLAUDE.md; agents need it for setup, dev server and validation commands")
    busy = fx._main_is_clean()
    line("ok" if not busy else "warn", "main checkout clean" if not busy else busy.splitlines()[0])
    for tool, why in (("node", "Node projects"), ("npm", "Node gates"), ("ruff", "Python lint gate"),
                      ("gitleaks", "secret scanning (built-in scan always runs)"), ("semgrep", "static analysis gate"),
                      ("npx", "headless screenshots for the experience reviewer")):
        line("ok" if shutil.which(tool) else "info", f"{tool}: {'found' if shutil.which(tool) else 'not found'} ({why})")
    print(f"worktrees go to {fx.worktree_root()}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- cli

def print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create a job, its branch and worktree")
    p.add_argument("feature", nargs="+")
    sub.add_parser("next", help="claim the first unchecked backlog item and create its job")
    p = sub.add_parser("set", help="update job.json fields (stage=, round=, reviews.<name>=, note=)")
    p.add_argument("job")
    p.add_argument("assignments", nargs="+")
    p = sub.add_parser("show", help="print job.json")
    p.add_argument("job")
    p = sub.add_parser("evidence", help="build the evidence pack and run gates for a round")
    p.add_argument("job")
    p.add_argument("round", type=int)
    p = sub.add_parser("check", help="check that a round is complete and report verdicts")
    p.add_argument("job")
    p.add_argument("round", type=int)
    p = sub.add_parser("merge", help="merge an approved job into the main branch")
    p.add_argument("job")
    p.add_argument("--human", action="store_true", help="a human approved it; skip the automatic risk gate")
    p = sub.add_parser("abandon", help="drop a job's worktree and branch")
    p.add_argument("job")
    sub.add_parser("port", help="print a free port on the configured dev host")
    sub.add_parser("status", help="list jobs")
    sub.add_parser("doctor", help="check that this repo is ready for the factory")
    p = sub.add_parser("serve", help="run the dashboard")
    p.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    try:
        fx = Factory(main_checkout())
        if args.command == "init":
            print_json(fx.init_job(" ".join(args.feature)))
        elif args.command == "next":
            print_json(fx.claim_next())
        elif args.command == "set":
            print_json(fx.update(args.job, args.assignments))
        elif args.command == "show":
            print_json(fx.load(args.job))
        elif args.command == "evidence":
            print_json(fx.evidence(args.job, args.round))
        elif args.command == "check":
            print_json(fx.check_round(args.job, args.round))
        elif args.command == "merge":
            print_json(fx.merge(args.job, human=args.human))
        elif args.command == "abandon":
            print_json(fx.abandon(args.job))
        elif args.command == "port":
            host = fx.cfg["dev_host"]
            port = free_port(host)
            shown = f"[{host}]" if ":" in host else host
            print_json({"host": host, "port": port, "url": f"http://{shown}:{port}"})
        elif args.command == "status":
            jobs = [job_summary(fx, d) for d in sorted(fx.jobs.glob("*")) if d.is_dir() and JOB_ID_RE.match(d.name)] if fx.jobs.is_dir() else []
            for j in filter(None, jobs):
                reviews = " ".join(f"{k}:{v}" for k, v in j["reviews"].items())
                print(f"{j['id']:<32} {j['stage']:<12} round {j['round']}  gates:{j.get('gates')}  {reviews}")
            if not any(jobs):
                print("no jobs")
        elif args.command == "doctor":
            return doctor(fx)
        elif args.command == "serve":
            serve(fx, args.port)
    except NeedsHuman as exc:
        print_json({"stage": "needs-human", "reason": str(exc)})
        return 3
    except FactoryError as exc:
        print(f"factory: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
