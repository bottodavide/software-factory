"""Shared fixtures: a throwaway git repo with the factory installed."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "factory" / "bin"))

import factory  # noqa: E402


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout


class RepoCase(unittest.TestCase):
    """Each test gets <tmp>/app (main checkout) with factory files committed."""

    gates: list = [{"name": "noop", "cmd": [sys.executable, "-c", "print('ok')"]}]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-test-")).resolve()
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "config", "user.email", "test@example.invalid", cwd=self.repo)
        sh("git", "config", "user.name", "Test", cwd=self.repo)
        sh("git", "config", "commit.gpgsign", "false", cwd=self.repo)
        shutil.copytree(ROOT / "factory" / "bin", self.repo / "factory" / "bin")
        shutil.copytree(ROOT / "factory" / "dashboard", self.repo / "factory" / "dashboard")
        shutil.copytree(ROOT / ".claude", self.repo / ".claude")
        cfg = json.loads((ROOT / "factory" / "config.json").read_text())
        cfg["gates"]["commands"] = self.gates
        (self.repo / "factory" / "config.json").write_text(json.dumps(cfg))
        (self.repo / "factory" / "backlog.md").write_text("# Backlog\n\n- [ ] Add a greeting helper\n- [ ] Second thing\n")
        (self.repo / ".gitignore").write_text("/factory/jobs/\n/factory/.backlog.lock\n__pycache__/\n")
        (self.repo / "AGENTS.md").write_text("# App\n\n## Setup\nnone\n\n## Dev server\nnone\n\n## Validation\nnone\n")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.js").write_text("export const one = 1\n")
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "init", cwd=self.repo)
        self.fx = factory.Factory(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit_in(self, worktree: Path, rel: str, text: str, msg: str = "change") -> None:
        path = worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sh("git", "add", rel, cwd=worktree)
        sh("git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "-q", "-m", msg, cwd=worktree)

    def write_reviews(self, job_id: str, rnd: int, verdict: str = "PASS") -> None:
        rdir = self.fx.job_dir(job_id) / f"round-{rnd}"
        rdir.mkdir(parents=True, exist_ok=True)
        for r in self.fx.reviewers:
            (rdir / f"review-{r}.md").write_text(f"VERDICT: {verdict}\n\n## Findings\nNone.\n")
