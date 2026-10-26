# librarian-mcp

[![grader self-check](https://github.com/Peacho97/librarian-mcp/actions/workflows/eval.yml/badge.svg)](https://github.com/Peacho97/librarian-mcp/actions/workflows/eval.yml)

An MCP server that gives coding agents whole-machine code retrieval. It is a thin
stdio client. The retrieval engine is a separate, private service that this
repository does not include.

Agent habits it supports:

- `librarian_brief` before starting work: what already exists, what to reuse, what is missing.
- `librarian_locate` before grep: current `file:line` from a code index, with a flag when a file changed recently.
- Two-way feedback: `librarian_tell` (supply knowledge the engine lacked or got wrong) and `librarian_gap` (report something it could not answer).

## Architecture

```
coding agent
    |  MCP over stdio
    v
librarian-mcp (this repo, mcp_server.py)
    |  HTTP + JSON, LIBRARIAN_URL
    v
private engine (runs on a local AI fleet)
    |
    +-- full-text index (FTS) over code, routes, config keys, symbols
    +-- embeddings index + catalog of tools and notes
```

The client holds no retrieval logic. It formats engine responses for the agent,
tags each call with a session id, retries connection failures, and keeps a
per-caller discussion history.

## Tools

| Tool | Purpose |
|------|---------|
| `librarian_brief(task)` | Start-of-task briefing |
| `librarian_locate(query)` | Exact location of a function, route, config key, entry or file |
| `librarian_ask(query)` | Grounded question answering |
| `librarian_discuss(message, reset)` | Multi-turn conversation, one thread per caller |
| `librarian_depends(entry_id)` | Dependents and dependencies of a catalog entry |
| `librarian_status()` | Engine and index health |
| `librarian_tell(subject, info)` | Give the engine knowledge |
| `librarian_gap(subject, missing)` | Report missing knowledge |
| `librarian_guide(text, exclude, expect, reset)` | Lead session registers guidance and exclusions for its workers |
| `librarian_session_log(limit)` | What this session's callers asked and were served |

## Install and register

Requires Python 3.12+.

```
pip install -e .
claude mcp add -s user librarian -- python /path/to/mcp_server.py
```

Environment:

- `LIBRARIAN_URL`: engine base URL, default `http://127.0.0.1:8000`; use any port you run the engine on.
- `LIBRARIAN_PROJECT`: optional project tag sent with each call.
- `CLAUDE_CODE_SESSION_ID`: used as the session id when present, otherwise a random id per launch.

## Pointing it at your own engine

Any HTTP service implementing the following contract works. All bodies are JSON.
Paths are under `$LIBRARIAN_URL`. Which tools send which tags:

- `brief`, `ask`, `discuss`, `tell`, `gap`: `session`, `project` (only when `LIBRARIAN_PROJECT` is set) and `caller` (only when set) in the JSON body.
- `locate`: `session` and optional `caller` as query parameters; no `project`.
- `guide`: `session` and optional `caller` in the body (or `session` query for DELETE).
- `session_log`: `session` query parameter.
- `depends`, `status`: no tags.

| Method and path | Request | Response fields read by the client |
|-----------------|---------|-----------------------------------|
| `GET /api/library/locate` | query: `q`, `limit` (10), `surface` (`mcp`), `session`, optional `caller` | `hits[]` with `kind`, `name`, `file`, `line`, `signature`, `stale`, `depends_on` (for `kind=entry`); `source`; `excluded_hits` (int); `footer` (string) |
| `POST /api/library/brief` | `{task, surface, session, project?, caller?}` | `brief`; or `busy`, `position`, `eta_s`; or `error` |
| `POST /api/library/ask` | `{query, surface, session, project?, caller?, history?[{role, content}]}` | `answer`, `provider`; or `busy`, `position`, `eta_s`; or `error` |
| `GET /api/library/depends` | query: `eid` | `dependents[]`, `depends_on[]` |
| `GET /api/library/status` | none | any JSON; `index.built_at` (epoch seconds) is used to compute index age |
| `POST /api/library/tell` | `{subject, info, source, session, project?, caller?}` | `stored` (string) or `error` |
| `POST /api/library/gap` | `{subject, missing, source, session, project?, caller?}` | `stored` (string) or `error` |
| `POST /api/library/guide` | `{session, text, exclude[], expect, caller?}` | `stored` (bool), `version`, `expires_at`; on rejection `reason` (or `detail`) |
| `DELETE /api/library/guide` | query: `session` | `cleared` (bool) |
| `GET /api/library/interactions` | query: `session`, `limit` | list, or object with `rows` / `interactions` / `items`; each row may carry `t`, `caller`, `kind`, `guidance_version`, `excluded_hits`, `query`, `served[]`, `provider`, `session` |

Error handling in the client: connection failures retry after 2, 4 and 8 seconds,
then report the engine as unreachable. A read timeout (30 s for GET, 180 s for
POST) is reported as a slow engine and is not retried. An HTTP error status is
reported as "engine answered N" and is not retried. `librarian_guide` uses a
separate path with a 30 s timeout and no retry; on a 4xx it passes the engine's JSON
body through so rejection reasons reach the agent.

## What the engine returns

Example `GET /api/library/locate?q=auth+middleware` response (synthetic):

```json
{
  "source": "index",
  "hits": [
    {"kind": "function", "name": "require_session", "file": "src/web/auth_middleware.py",
     "line": 24, "signature": "(request)", "stale": false},
    {"kind": "entry", "name": "auth-middleware", "file": "", "depends_on": "session-store"}
  ],
  "excluded_hits": 0
}
```

The client renders this as:

```
Locations for 'auth middleware' (source: index):
- [function] require_session -> src/web/auth_middleware.py:24  (request)
- [entry] auth-middleware ->
    depends_on: session-store
```

## Eval

`eval/questions.jsonl` holds 22 questions about a fictional web application, across
the skills `locate`, `ask_librarian`, `brief` and `depends`. Each has
`expect.any_of`: a case-insensitive substring match against the answer decides
pass or fail. `eval/run.py` prints per-question results and the score, and exits 1
if the score is below 90%.

```
python eval/run.py                                         # live engine at LIBRARIAN_URL
python eval/run.py --fixture eval/fixtures/answers.jsonl   # offline
```

What CI measures: the offline run answers from hand-authored fixture answers in
`eval/fixtures/answers.jsonl` that contain the expected substrings. It scores 100%
by construction. The fixture gate proves the grader and runner work; it does not
measure retrieval quality. The 86.4% to 100% history was measured on the original
private question set against the private engine, so it does not apply to the
published questions; see `eval/HISTORY.md`.

To evaluate your own engine, edit the questions and `expect` strings to match your
corpus and run the live mode.

## Tests

```
python -m unittest discover tests
```

## License

MIT. See `LICENSE`.
