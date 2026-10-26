"""
librarian-mcp: stdio MCP server that proxies to a retrieval engine over HTTP.

The engine (any service implementing the /api/library/* contract in README.md)
does the retrieval; this client only adapts it to MCP tools. Engine base URL
comes from LIBRARIAN_URL (default http://127.0.0.1:8000).

Register:
    claude mcp add -s user librarian -- python /path/to/mcp_server.py
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

from mcp.server.fastmcp import FastMCP

ENGINE_URL = os.environ.get("LIBRARIAN_URL", "http://127.0.0.1:8000").rstrip("/")
mcp = FastMCP("librarian")

# This server is a per-session child process, so one stable id per launch tags
# all of THIS session's librarian interactions in the engine's interaction
# log. Prefer Claude Code's real session id when present.
_SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID") or ("sess-" + uuid.uuid4().hex[:8])
# Optional project tag: env LIBRARIAN_PROJECT, else untagged.


def _current_project() -> str:
    return os.environ.get("LIBRARIAN_PROJECT", "").strip()


def _tag(body: dict, caller: str = "") -> dict:
    """Attach session + project (+ caller, when stamped) attribution to an engine payload."""
    body.setdefault("session", _SESSION_ID)
    proj = _current_project()
    if proj:
        body.setdefault("project", proj)
    if caller:
        body["caller"] = caller
    return body


def _is_timeout(e: Exception) -> bool:
    """READ-phase timeout: the engine accepted the connection and is still thinking
    (a slow answer can take minutes): the engine is UP.

    urllib wraps connect/send-phase OSErrors (incl. a connect timeout) in
    URLError(reason=...); a timeout from getresponse()/read() surfaces bare.
    So only a BARE timeout counts here; URLError(reason=timeout) is a connect
    failure and stays retryable + engine-down."""
    if isinstance(e, urllib.error.URLError):
        return False
    return isinstance(e, (socket.timeout, TimeoutError))


def _down(e: Exception, timeout: float | None = None) -> dict:
    msg = f"engine unreachable ({e}). Is the engine running at {ENGINE_URL}?"
    # Only treat it as "engine down" when the connection
    # itself failed; an HTTP status means the engine is up and answering, and a
    # read timeout means it is up but slow (do not report it as an outage).
    if isinstance(e, urllib.error.HTTPError):
        return {
            "_error": f"engine answered {e.code} for this call ({e}). "
            "Engine is up; the request itself failed."
        }
    if _is_timeout(e):
        after = f" after {int(timeout)}s" if timeout else ""
        return {
            "_error": f"engine reachable but the call timed out{after} - the "
            "engine is busy or slow. Not retried. Try again or ask a narrower question."
        }
    return {"_error": msg}


def _retryable(e: Exception) -> bool:
    """Connection-level failures only - an engine restart window looks like
    connection refused for ~5-10s. HTTP status answers mean the engine is up,
    and so does a read timeout (retrying a slow call just stacks minutes)."""
    if isinstance(e, urllib.error.HTTPError) or _is_timeout(e):
        return False
    return isinstance(e, (urllib.error.URLError, ConnectionError, OSError, TimeoutError))


def _open_with_retry(req, timeout: float) -> dict:
    """urlopen with 3 retries (2s/4s/8s) before declaring the engine down, so a
    normal engine restart doesn't strand sessions."""
    import time as _time

    last: Exception | None = None
    for _i, delay in enumerate((0, 2, 4, 8)):
        if delay:
            _time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            if not _retryable(e):
                break
    assert last is not None
    return _down(last, timeout)


def _get(path: str) -> dict:
    return _open_with_retry(ENGINE_URL + path, timeout=30)


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ENGINE_URL + path, data=data, headers={"Content-Type": "application/json"}
    )
    return _open_with_retry(req, timeout=180)


def _call_body(method: str, path: str, body: dict | None = None) -> dict:
    """Like _get/_post, but a 4xx/5xx answer returns the engine's JSON body (with
    `_status`) instead of a generic error, so rejection reasons reach the caller
    verbatim (librarian_guide needs the engine's `reason`)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(ENGINE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode("utf-8", "replace"))
            if not isinstance(res, dict):
                res = {"reason": str(res)}
        except Exception:
            res = {"reason": f"engine answered {e.code}"}
        # FastAPI HTTPException wraps it as {"detail": ...}
        if "reason" not in res and "detail" in res:
            d = res["detail"]
            res = d if isinstance(d, dict) else {"reason": str(d)}
        res["_status"] = e.code
        return res
    except Exception as e:
        return _down(e, 30)


def _local_hhmm(ts) -> str:
    """Epoch seconds or ISO string -> local HH:MM ('?' if unparseable)."""
    import datetime as _dt

    if ts is None or ts == "":
        return "?"
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        pass
    try:
        d = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%H:%M")
    except Exception:
        return str(ts)[:16]


def _busy_text(res: dict) -> str | None:
    """Engine-side ask queue said 'at capacity'. Render an honest, actionable line."""
    if not res.get("busy"):
        return None
    pos = res.get("position")
    eta = res.get("eta_s")
    bits = ["Librarian busy - other sessions hold all ask slots."]
    if pos:
        bits.append(f"You were position {pos} in line.")
    if eta is not None:
        bits.append(f"A slot is expected in ~{int(eta)}s.")
    bits.append("Retry shortly (the queue is fair: FIFO + priority aging).")
    return " ".join(bits)


@mcp.tool()
def librarian_locate(query: str, caller: str = "") -> str:
    """Find the EXACT, current location of something on this PC (file:line) - a
    function, route, config key, catalog entry, tool, or file. AI sessions should
    USE THIS BEFORE grep-scanning the codebase; the answer is generated from a fresh code index,
    so line numbers are current and a [changed] flag warns when a file was just
    edited. Pass a natural query like 'where is the catalog search code' or
    'route that handles checkout'. `caller` is an optional caller label; leave it empty."""
    q = urllib.parse.quote(query)
    path = f"/api/library/locate?q={q}&limit=10&surface=mcp&session={urllib.parse.quote(_SESSION_ID)}"
    if caller:
        path += f"&caller={urllib.parse.quote(caller)}"
    res = _get(path)
    if res.get("_error"):
        return res["_error"]
    hits = res.get("hits", [])
    try:
        withheld = int(res.get("excluded_hits") or 0)
    except (TypeError, ValueError):
        withheld = 0
    withheld_line = f"({withheld} hit(s) withheld by exclusions)" if withheld > 0 else ""
    if res.get("footer"):  # engine-built guidance footer (carries the withheld count)
        withheld_line = res["footer"]
    if not hits:
        msg = f"No matches for '{query}'."
        return f"{msg}\n{withheld_line}" if withheld_line else msg
    lines = [f"Locations for '{query}' (source: {res.get('source')}):"]
    for h in hits:
        loc = h.get("file", "") or ""
        if h.get("line"):
            loc += f":{h['line']}"
        flag = " [changed - re-verify]" if h.get("stale") else ""
        sig = f"  {h['signature']}" if h.get("signature") else ""
        lines.append(f"- [{h.get('kind')}] {h.get('name')} -> {loc}{sig}{flag}")
        if h.get("kind") == "entry" and h.get("depends_on"):
            lines.append(f"    depends_on: {h['depends_on']}")
    if withheld_line:
        lines.append(withheld_line)
    return "\n".join(lines)


@mcp.tool()
def librarian_brief(task: str, caller: str = "") -> str:
    """START-OF-TASK BRIEF for AI sessions. Call this FIRST when beginning any non-trivial task:
    pass the task/goal and the librarian reviews everything that already exists on
    this PC and tells you what's relevant, HOW it helps here, what to reuse, and -
    honestly - what we DON'T have. Lets you start with most of what you need
    instead of hunting. If we have nothing, it says so plainly. If the coordinating session registered guidance (librarian_guide), the answer already accounts for it and shows its version. `caller` is an optional caller label; leave it empty."""
    res = _post("/api/library/brief", _tag({"task": task, "surface": "mcp"}, caller))
    if res.get("_error"):
        return res["_error"]
    busy = _busy_text(res)
    if busy:
        return busy
    return res.get("brief", "") or res.get("error", "no brief")


@mcp.tool()
def librarian_ask(query: str, caller: str = "") -> str:
    """Ask the librarian a question about this PC's systems: what exists, where it
    lives, which tool to use, or how something works. Returns a grounded answer
    (WHAT / WHERE file:line / TOOLS / NEXT) over the whole-PC corpus (catalog +
    tools + machine map + project memory + a live code index). AI sessions should
    USE THIS instead of guessing, saying they can't find something, or calling a
    premium model just to rediscover local context. If the coordinating session registered guidance (librarian_guide), the answer already accounts for it and shows its version. `caller` is an optional caller label; leave it empty."""
    res = _post("/api/library/ask", _tag({"query": query, "surface": "mcp"}, caller))
    if res.get("_error"):
        return res["_error"]
    busy = _busy_text(res)
    if busy:
        return busy
    ans = res.get("answer", "") or res.get("error", "no answer")
    if res.get("provider") and res["provider"] not in ("claude", "none"):
        ans = f"(via {res['provider']})\n{ans}"
    return ans


@mcp.tool()
def librarian_depends(entry_id: str, caller: str = "") -> str:
    """Show what depends on / connects to a catalog entry id (the 'what breaks if
    I change X' graph). Use after librarian_locate returns an [entry] to see its
    blast radius. Pass the entry id, e.g. 'payments-module'. `caller` is an optional caller label; leave it empty."""
    res = _get(f"/api/library/depends?eid={urllib.parse.quote(entry_id)}")
    if res.get("_error"):
        return res["_error"]
    return (
        f"{entry_id}\n  used by (dependents): {', '.join(res.get('dependents', [])) or '-'}"
        f"\n  uses (depends_on/connects_to): {', '.join(res.get('depends_on', [])) or '-'}"
    )


@mcp.tool()
def librarian_status(caller: str = "") -> str:
    """Health of the librarian: catalog availability + index counts (files,
    symbols, routes, config keys, entries, changed files) + index age. `caller` is an optional caller label; leave it empty."""
    res = _get("/api/library/status")
    if res.get("_error"):
        return res["_error"]
    try:
        import time

        built = (res.get("index") or {}).get("built_at")
        if built:
            age_h = (time.time() - float(built)) / 3600
            res["index"]["index_age"] = f"{age_h * 60:.0f}m" if age_h < 1 else f"{age_h:.1f}h"
    except Exception:
        pass
    return json.dumps(res, indent=2)


# --- the two-way channel ---------------------------------------------------------
# This MCP server is a per-session child process, so module state = the private
# conversations between THIS session and the librarian - one thread per caller
# ("" = the coordinating session; each worker caller label gets its own thread), so a
# worker never sees the coordinator's turns and vice versa.
_DISCUSSION: dict[str, list[dict]] = {}


@mcp.tool()
def librarian_discuss(message: str, reset: bool = False, caller: str = "") -> str:
    """DISCUSS with the librarian - a running two-way conversation (it remembers
    your prior turns). Use for hashing out a task together: verify your
    understanding of the project, ask about paths/tools, and ANSWER the questions
    it asks back (its ASK: line). Multiple trips are encouraged - keep going until
    both sides agree the picture is complete. Pass reset=true to start a fresh
    discussion for a new topic. Threads are per caller: workers do not see the
    the coordinator's turns (and reset clears only your own thread). `caller` is an optional caller label; leave it empty."""
    key = caller or ""
    if reset:
        _DISCUSSION[key] = []
    thread = _DISCUSSION.setdefault(key, [])
    res = _post(
        "/api/library/ask",
        _tag({"query": message, "history": thread[-10:], "surface": "mcp"}, caller),
    )
    if res.get("_error"):
        return res["_error"]
    busy = _busy_text(res)
    if busy:
        return busy
    ans = res.get("answer", "") or res.get("error", "no answer")
    thread.append({"role": "user", "content": message})
    thread.append({"role": "assistant", "content": ans})
    _DISCUSSION[key] = thread[-12:]
    if res.get("provider") and res["provider"] not in ("claude", "none"):
        ans = f"(via {res['provider']})\n{ans}"
    return ans


@mcp.tool()
def librarian_guide(
    text: str,
    exclude: list[str] | None = None,
    expect: str = "",
    reset: bool = False,
    caller: str = "",
) -> str:
    """For the coordinating session, before delegating to workers: register this
    session's intent so every worker's librarian_brief / librarian_ask /
    librarian_discuss is steered by it (their answers show the guidance version).
    Freeform intent about code/projects only - never client names, emails,
    health, or personal data (the engine rejects PII). Replaced on every call (version
    bumps); expires after 6h. reset=true clears it first (reset with empty text =
    just clear). Re-guide per phase.

    `exclude`: path prefixes/substrings, project names, or catalog entry ids the
    librarian must NOT serve this session. They are hard-filtered on the engine
    before the model sees them, and librarian_locate hits are filtered too. Use
    it to stop cross-project bleed, e.g. another app's UI palette/layout leaking
    into a worker's build (["apps/other-app", "legacy-service"]). Max 40 items,
    200 chars each.
    `expect`: one line (<=500 chars) predicting the effect this guidance should
    have on worker output; workers quote the guidance footer in their
    report, and the coordinator compares that against this prediction.
    Replace-on-write: every call replaces text, exclude AND expect, so a call
    without `exclude` clears the exclusions. `caller` is an optional caller label; leave it empty."""
    sess = urllib.parse.quote(_SESSION_ID)
    out = []
    if reset:
        res = _call_body("DELETE", f"/api/library/guide?session={sess}")
        if res.get("_error"):
            return res["_error"]
        if res.get("_status"):
            return res.get("reason") or json.dumps(res)
        out.append("cleared" if res.get("cleared") else "nothing to clear")
    if text and text.strip():
        excl = [str(x) for x in (exclude or []) if str(x).strip()]
        body = {"session": _SESSION_ID, "text": text, "exclude": excl, "expect": expect or ""}
        if caller:
            body["caller"] = caller
        res = _call_body("POST", "/api/library/guide", body)
        if res.get("_error"):
            return res["_error"]
        if not res.get("stored"):
            out.append(res.get("reason") or json.dumps(res))
        else:
            out.append(
                f"stored v{res.get('version')}, expires {_local_hhmm(res.get('expires_at'))} - "
                f"{len(excl)} exclusion(s), expect: {'yes' if (expect or '').strip() else 'no'} - "
                "workers' librarian_brief/ask/discuss/locate now carry it"
            )
    if not out:
        return "nothing to do: pass text (and/or reset=true)"
    return "; ".join(out)


@mcp.tool()
def librarian_session_log(limit: int = 30, caller: str = "") -> str:
    """For the coordinating session when judging a worker report: what this session's
    workers (and the coordinator) asked the librarian and what they were served - one
    line per interaction, newest last, with caller, kind, guidance version,
    withheld-hit count (xN, only when exclusions filtered something), query,
    cite count and provider. `caller` is an optional caller label; leave it empty."""
    sess = urllib.parse.quote(_SESSION_ID)
    res = _get(f"/api/library/interactions?session={sess}&limit={int(limit)}")
    if isinstance(res, dict) and res.get("_error"):
        return res["_error"]
    if isinstance(res, list):
        rows = res
    else:
        rows = res.get("rows") or res.get("interactions") or res.get("items") or []
    # Drop internal context lookups (librarian_ask writes a sessionless kind:"locate" row).
    rows = [r for r in rows if not (r.get("kind") == "locate" and not r.get("session"))]

    def _tkey(r):
        try:
            return (0, float(r.get("t") or 0), "")
        except Exception:
            return (1, 0.0, str(r.get("t")))

    rows.sort(key=_tkey)
    workers = [r for r in rows if r.get("caller")]
    callers = {r.get("caller") for r in workers}

    def _xh(r) -> int:
        try:
            return int(r.get("excluded_hits") or 0)
        except (TypeError, ValueError):
            return 0

    total_withheld = sum(_xh(r) for r in rows)
    lines = [
        f"session {_SESSION_ID}: {len(rows)} rows ({len(workers)} worker, "
        f"{len(rows) - len(workers)} coordinator), {len(callers)} distinct worker caller(s), "
        f"{total_withheld} hit(s) withheld by exclusions"
    ]
    for r in rows:
        q = " ".join(str(r.get("query") or "").split())[:90]
        gv = r.get("guidance_version")
        xh = _xh(r)
        xs = f" x{xh}" if xh else ""
        lines.append(
            f"{_local_hhmm(r.get('t'))}  {r.get('caller') or 'COORD'}  {r.get('kind') or '?'}  "
            f"gv{gv if gv is not None else '-'}{xs}  {q}  -> {len(r.get('served') or [])} cites / "
            f"{r.get('provider') or '?'}"
        )
    return "\n".join(lines)


@mcp.tool()
def librarian_tell(subject: str, info: str, caller: str = "") -> str:
    """GIVE the librarian knowledge it doesn't have - something you built or
    discovered this session, a decision made, a path/tool it didn't know, or
    context you think matters even if the librarian might not. It is ALWAYS
    accepted (never rejected as irrelevant) and offered to future sessions.
    Use whenever you know something the librarian got wrong or lacked. `caller` is an optional caller label; leave it empty."""
    res = _post("/api/library/tell", _tag({"subject": subject, "info": info, "source": "mcp"}, caller))
    if res.get("_error"):
        return res["_error"]
    return res.get("stored") or res.get("error", "not stored")


@mcp.tool()
def librarian_gap(subject: str, missing: str, caller: str = "") -> str:
    """REPORT a gap - the librarian was missing something you needed (and you
    don't have the answer yourself, otherwise use librarian_tell). It goes on the
    librarian's gap ledger; every review it re-hunts open gaps and escalates the
    still-unanswerable ones to the operator. This is how the librarian learns what to
    go back and get. `caller` is an optional caller label; leave it empty."""
    res = _post("/api/library/gap", _tag({"subject": subject, "missing": missing, "source": "mcp"}, caller))
    if res.get("_error"):
        return res["_error"]
    return res.get("stored") or res.get("error", "not stored")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
