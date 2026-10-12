from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from helpers import RepoCase, factory


class Dashboard(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        job = self.fx.init_job("Add a greeting helper")
        self.job_id = job["id"]
        (self.fx.job_dir(self.job_id) / "spec.md").write_text("# Spec <script>x</script>\n")
        handler = factory.make_handler(self.fx, {"127.0.0.1", "localhost"})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path: str, host: str | None = None) -> tuple[int, bytes, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        body = res.read()
        conn.close()
        return res.status, body, dict(res.getheaders())

    def test_index_and_api(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Software Factory", body)
        self.assertIn("Content-Security-Policy", headers)
        status, body, _ = self.get("/api/jobs")
        jobs = json.loads(body)["jobs"]
        self.assertEqual([j["id"] for j in jobs], [self.job_id])
        self.assertIn("spec.md", jobs[0]["files"])

    def test_file_is_served_as_plain_text(self):
        status, body, headers = self.get(f"/api/file?job={self.job_id}&path=spec.md")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))

    def test_traversal_and_bad_input_rejected(self):
        for path in (
            f"/api/file?job={self.job_id}&path=../../../.gitignore",
            f"/api/file?job={self.job_id}&path=/etc/passwd",
            "/api/file?job=../x&path=spec.md",
            f"/api/file?job={self.job_id}&path=job.py",
        ):
            self.assertIn(self.get(path)[0], (400, 404), path)

    def test_foreign_host_rejected(self):
        self.assertEqual(self.get("/api/jobs", host="attacker.example")[0], 403)

    def test_missing_dashboard_does_not_crash(self):
        (Path(self.repo) / "factory" / "dashboard" / "index.html").unlink()
        self.assertEqual(self.get("/")[0], 500)


if __name__ == "__main__":
    unittest.main()
