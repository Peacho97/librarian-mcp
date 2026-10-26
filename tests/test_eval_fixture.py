"""The offline fixture eval scores 100%, and the grader fails a wrong fixture."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "eval" / "run.py"
FIXTURE = ROOT / "eval" / "fixtures" / "answers.jsonl"


def _run(fixture: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN), "--fixture", str(fixture)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class FixtureEval(unittest.TestCase):
    def test_fixture_scores_100(self):
        r = _run(FIXTURE)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Score: 22/22 = 100%", r.stdout)

    def test_wrong_fixture_fails_gate(self):
        rows = [json.loads(ln) for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln]
        for r in rows:
            r["answer"] = "zzz unrelated"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            r = _run(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
