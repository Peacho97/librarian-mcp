# Eval history

Two measured runs, both on 2026-07-01 (local time), scored on the original
private question set against the private engine:

| Run | Score | Failed ids |
|-----|-------|------------|
| 1 (about 20:35) | 19/22 = 86.4% | 2, 5, 8 |
| 2 (about 21:23, after prompt and index changes) | 22/22 = 100% | none |

The questions published in this repo are a rewritten generic set about a fictional
codebase. The 86.4% and 100% figures were not measured on the published set, so
they do not apply to it.

The fixture mode (`--fixture eval/fixtures/answers.jsonl`) uses hand-authored
answers. It checks that the grader, the runner and the 90% gate work. It does not
measure retrieval quality.
