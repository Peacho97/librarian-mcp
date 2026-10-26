"""
eval/run.py: graded eval for the librarian engine contract.

Runs every question in questions.jsonl, grades each answer by case-insensitive
substring match against expect.any_of, and prints score and per-question
PASS/FAIL.

    python eval/run.py                                          # live engine (LIBRARIAN_URL)
    python eval/run.py --fixture eval/fixtures/answers.jsonl    # offline, no engine
    python eval/run.py --ids 3,7 --skill locate

Fixture mode answers from recorded {"id", "answer"} rows. It is a regression
check on the question set and grader, not a retrieval-quality measurement.
Live mode appends a summary row to results.jsonl (git-ignored).

Exit code: 0 if score >= 90%, 1 otherwise. Stdlib only.
"""


from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252; a Unicode char in a model answer must
# not crash the runner (it corrupted the exit code once: score 95%, exit 1).
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENGINE_URL = os.environ.get("LIBRARIAN_URL", "http://127.0.0.1:8000").rstrip("/")
HERE = Path(__file__).parent
QUESTIONS = HERE / "questions.jsonl"
RESULTS = HERE / "results.jsonl"
PASS_BAR = 0.90


def _get(path: str, timeout: float = 60) -> dict:
    with urllib.request.urlopen(ENGINE_URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post(path: str, body: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(
        ENGINE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# skill -> how to ask the engine. Everything LLM-shaped goes through /ask; locate
# and status are deterministic (no LLM) and must pass without one.
def run_question(q: dict) -> str:
    skill = q.get("skill", "ask_librarian")
    query = q["query"]
    if skill == "locate":
        res = _get(f"/api/library/locate?q={urllib.parse.quote(query)}&limit=10&surface=eval")
        return json.dumps(res)
    if skill == "catalog_status":
        return json.dumps(_get("/api/library/status"))
    if skill == "depends":
        return json.dumps(_get(f"/api/library/depends?eid={urllib.parse.quote(query)}"))
    if skill == "brief":
        res = _post("/api/library/brief", {"task": query, "surface": "eval"})
        return res.get("brief", "") or json.dumps(res)
    # ask_librarian / what_tools / propose_update -> the LLM front desk
    res = _post("/api/library/ask", {"query": query, "surface": "eval"})
    return res.get("answer", "") or json.dumps(res)


def grade(answer: str, expect: dict) -> bool:
    hay = answer.lower()
    return any(str(s).lower() in hay for s in expect.get("any_of", []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated question ids to run")
    ap.add_argument("--skill", help="only questions of this skill")
    ap.add_argument("--fixture", help="answers.jsonl to grade offline instead of calling the engine")
    args = ap.parse_args()

    fixture = None
    if args.fixture:
        fixture = {
            r["id"]: r["answer"]
            for r in (
                json.loads(ln)
                for ln in Path(args.fixture).read_text(encoding="utf-8").splitlines()
                if ln.strip()
            )
        }

    if fixture is None:
        try:
            _get("/api/library/status", timeout=30)
        except Exception as e:
            print(f"FATAL: engine unreachable at {ENGINE_URL} ({e}).")
            return 1

    questions = [
        json.loads(ln) for ln in QUESTIONS.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if args.ids:
        want = {int(x) for x in args.ids.split(",")}
        questions = [q for q in questions if q.get("id") in want]
    if args.skill:
        questions = [q for q in questions if q.get("skill") == args.skill]

    passed, failures = 0, []
    for q in questions:
        t0 = time.time()
        try:
            if fixture is not None:
                answer = fixture[q["id"]]
            else:
                answer = run_question(q)
            ok = grade(answer, q.get("expect", {}))
        except Exception as e:
            answer, ok = f"(error: {e})", False
        dt = time.time() - t0
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] #{q.get('id')} ({q.get('skill')}, {dt:.1f}s) {q['query'][:70]}")
        if ok:
            passed += 1
        else:
            failures.append((q, answer))

    total = len(questions)
    score = passed / total if total else 0.0
    print(f"\nScore: {passed}/{total} = {score:.0%}  (bar: {PASS_BAR:.0%})")

    for q, answer in failures:
        print(
            f"\n--- FAIL #{q.get('id')}: {q['query']}\n"
            f"    expected any of: {q.get('expect', {}).get('any_of')}\n"
            f"    got: {answer[:600]}"
        )

    try:
        if fixture is not None:
            return 0 if score >= PASS_BAR else 1  # fixture runs are not history
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "t": round(time.time(), 1),
                        "passed": passed,
                        "total": total,
                        "score": round(score, 3),
                        "failed_ids": [q.get("id") for q, _ in failures],
                    }
                )
                + "\n"
            )
    except Exception:
        pass

    return 0 if score >= PASS_BAR else 1


if __name__ == "__main__":
    sys.exit(main())
