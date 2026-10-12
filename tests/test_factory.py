from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from helpers import RepoCase, factory, sh


class PureFunctions(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(factory.slugify("Add a 404 page for unknown routes"), "404-page-unknown-routes")
        self.assertEqual(factory.slugify("!!!"), "job")
        self.assertLessEqual(len(factory.slugify("word " * 50)), 40)

    def test_secret_scan_finds_real_and_skips_placeholders(self):
        diff = "\n".join([
            "+++ b/src/config.js",
            "+const key = 'AKIAABCDEFGHIJKLMNOP'",
            "+password = 'example-password-123'",
            "+token: process.env.TOKEN",
            "+api_key = \"9f8e7d6c5b4a39281706\"",
            " unchanged AKIAABCDEFGHIJKLMNOP",
        ])
        found = factory.scan_secrets(diff)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all(f.startswith("src/config.js") for f in found))
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", " ".join(found))

    def test_matches_is_case_insensitive(self):
        self.assertTrue(factory.matches("SRC/Auth/Login.ts", ["*/auth/*"]))


class Lifecycle(RepoCase):
    def test_init_creates_branch_worktree_and_state(self):
        job = self.fx.init_job("Add a greeting helper")
        self.assertEqual(job["id"], "001-greeting-helper")
        self.assertTrue(Path(job["worktree"]).is_dir())
        self.assertEqual(Path(job["worktree"]).parent, self.tmp / "app-factory")
        self.assertEqual(sh("git", "branch", "--show-current", cwd=Path(job["worktree"])).strip(), job["branch"])
        self.assertEqual(self.fx.load(job["id"])["stage"], "spec")
        self.assertEqual(self.fx.init_job("Second")["id"][:3], "002")

    def test_init_requires_gitignored_jobs(self):
        (self.repo / ".gitignore").write_text("")
        with self.assertRaises(factory.FactoryError):
            self.fx.init_job("anything")

    def test_set_validates(self):
        job = self.fx.init_job("x feature")
        self.fx.update(job["id"], ["stage=review", "round=1", "reviews.code=PASS"])
        state = self.fx.load(job["id"])
        self.assertEqual((state["stage"], state["round"], state["reviews"]["code"]), ("review", 1, "PASS"))
        for bad in (["stage=shipping"], ["reviews.nobody=PASS"], ["reviews.code=MAYBE"], ["round=x"]):
            with self.assertRaises(factory.FactoryError):
                self.fx.update(job["id"], bad)

    def test_claim_next_marks_backlog(self):
        job = self.fx.claim_next()
        text = (self.repo / "factory" / "backlog.md").read_text()
        self.assertIn(f"- [x] Add a greeting helper → {job['id']}", text)
        self.assertIn("- [ ] Second thing", text)

    def test_happy_path_merges_autonomously(self):
        job = self.fx.init_job("Add a greeting helper")
        wt = Path(job["worktree"])
        self.commit_in(wt, "src/greet.js", "export const greet = (n) => `hi ${n}`\n")
        ev = self.fx.evidence(job["id"], 1)
        self.assertFalse(ev["blocking_failed"], ev)
        self.assertEqual(ev["risk"], [])
        self.write_reviews(job["id"], 1)
        self.assertTrue(self.fx.check_round(job["id"], 1)["all_pass"])
        (self.fx.job_dir(job["id"]) / "decision.md").write_text("APPROVE\n\n## Why\nok\n")
        result = self.fx.merge(job["id"])
        self.assertEqual(result["stage"], "merged")
        self.assertTrue((self.repo / "src" / "greet.js").exists())
        self.assertFalse(wt.exists())
        self.assertNotIn(job["branch"], sh("git", "branch", cwd=self.repo))

    def test_risky_change_goes_to_human(self):
        job = self.fx.init_job("Bump deps")
        self.commit_in(Path(job["worktree"]), "package.json", '{"name": "app"}\n')
        ev = self.fx.evidence(job["id"], 1)
        self.assertIn("dependencies", ev["risk"])
        self.write_reviews(job["id"], 1)
        (self.fx.job_dir(job["id"]) / "decision.md").write_text("APPROVE\n")
        with self.assertRaises(factory.NeedsHuman):
            self.fx.merge(job["id"])
        self.assertEqual(self.fx.load(job["id"])["stage"], "needs-human")
        self.assertEqual(self.fx.merge(job["id"], human=True)["stage"], "merged")

    def test_branch_moved_after_review_is_refused(self):
        job = self.fx.init_job("Greeting")
        wt = Path(job["worktree"])
        self.commit_in(wt, "src/a.js", "1\n")
        self.fx.evidence(job["id"], 1)
        self.commit_in(wt, "src/b.js", "2\n", "sneaky")
        self.write_reviews(job["id"], 1)
        (self.fx.job_dir(job["id"]) / "decision.md").write_text("APPROVE\n")
        with self.assertRaisesRegex(factory.NeedsHuman, "branch moved"):
            self.fx.merge(job["id"])

    def test_changes_verdict_blocks_merge(self):
        job = self.fx.init_job("Greeting")
        self.commit_in(Path(job["worktree"]), "src/a.js", "1\n")
        self.fx.evidence(job["id"], 1)
        self.write_reviews(job["id"], 1, "CHANGES")
        (self.fx.job_dir(job["id"]) / "decision.md").write_text("APPROVE\n")
        with self.assertRaisesRegex(factory.NeedsHuman, "not all PASS"):
            self.fx.merge(job["id"])

    def test_dirty_main_checkout_blocks_merge(self):
        job = self.fx.init_job("Greeting")
        self.commit_in(Path(job["worktree"]), "src/a.js", "1\n")
        self.fx.evidence(job["id"], 1)
        self.write_reviews(job["id"], 1)
        (self.fx.job_dir(job["id"]) / "decision.md").write_text("APPROVE\n")
        (self.repo / "src" / "app.js").write_text("dirty\n")
        with self.assertRaisesRegex(factory.NeedsHuman, "uncommitted"):
            self.fx.merge(job["id"])

    def test_secret_in_diff_fails_blocking_gate(self):
        job = self.fx.init_job("Config")
        self.commit_in(Path(job["worktree"]), "src/cfg.js", "const k = 'AKIAABCDEFGHIJKLMNOP'\n")
        ev = self.fx.evidence(job["id"], 1)
        self.assertTrue(ev["blocking_failed"])
        self.assertEqual(ev["gates"]["secrets"], "fail")

    def test_human_merge_requires_needs_human_and_green_gates(self):
        job = self.fx.init_job("Greeting")
        self.commit_in(Path(job["worktree"]), "src/a.js", "1\n")
        self.fx.evidence(job["id"], 1)
        with self.assertRaisesRegex(factory.FactoryError, "only a job waiting for a human"):
            self.fx.merge(job["id"], human=True)
        self.assertEqual(self.fx.load(job["id"])["stage"], "spec")  # not moved by the refused call

    def test_terminal_stages_cannot_be_set(self):
        job = self.fx.init_job("Greeting")
        for stage in ("merged", "abandoned"):
            with self.assertRaises(factory.FactoryError):
                self.fx.update(job["id"], [f"stage={stage}"])

    def test_rework_budget(self):
        job = self.fx.init_job("Greeting")
        self.commit_in(Path(job["worktree"]), "src/a.js", "1\n")
        self.assertTrue(self.fx.evidence(job["id"], 2)["rework_allowed"])
        self.assertFalse(self.fx.evidence(job["id"], 3)["rework_allowed"])
        self.fx.update(job["id"], ["rework_base=3"])
        self.assertTrue(self.fx.evidence(job["id"], 4)["rework_allowed"])

    def test_abandon_cleans_up(self):
        job = self.fx.init_job("Throwaway")
        self.assertEqual(self.fx.abandon(job["id"])["stage"], "abandoned")
        self.assertFalse(Path(job["worktree"]).exists())

    def test_cli_runs_from_inside_worktree(self):
        job = self.fx.init_job("Cli check")
        out = sh(sys.executable, str(self.repo / "factory" / "bin" / "factory.py"), "show", job["id"], cwd=Path(job["worktree"]))
        self.assertEqual(json.loads(out)["id"], job["id"])


class FailingGate(RepoCase):
    gates = [{"name": "tests", "run": "exit 1"}, {"name": "audit", "run": "exit 1", "blocking": False}]

    def test_gate_results(self):
        job = self.fx.init_job("Something")
        self.commit_in(Path(job["worktree"]), "src/a.js", "1\n")
        ev = self.fx.evidence(job["id"], 1)
        self.assertTrue(ev["blocking_failed"])
        self.assertIn("advisory-gate-failed", ev["risk"])
        self.assertEqual(self.fx.check_round(job["id"], 1)["gates"], "CHANGES")

    def test_human_cannot_merge_failing_gates(self):
        job = self.fx.init_job("Something")
        self.commit_in(Path(job["worktree"]), "src/a.js", "1\n")
        self.fx.evidence(job["id"], 1)
        self.fx.update(job["id"], ["stage=needs-human"])
        with self.assertRaisesRegex(factory.NeedsHuman, "blocking quality gates"):
            self.fx.merge(job["id"], human=True)


if __name__ == "__main__":
    unittest.main()
