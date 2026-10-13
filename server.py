#!/usr/bin/env python3
"""
skill_librarian_mcp — a librarian that mediates between agents and a skill collection.

The collection stays raw text on disk. Agents never load it.
They ask the librarian; the librarian retrieves, diversifies, and learns from outcomes.

Tools:
  librarian_find      — recommend skills for an intent (MMR-diverse, recency-decayed)
  librarian_brainstorm — wider, looser sweep with wildcards for ideation
  librarian_report    — agents report back whether a skill worked (append-only)
  librarian_reindex   — rescan + re-embed changed SKILL.md files
  librarian_stats     — usage digest: hot skills, dead skills, gap queries

Config (env):
  LIBRARIAN_SKILLS_DIR   default: ~/.claude/skills
  LIBRARIAN_DB           default: ~/.skill_librarian/librarian.db
  OLLAMA_HOST            default: http://localhost:11434
  LIBRARIAN_EMBED_MODEL  default: nomic-embed-text
"""

import asyncio
import hashlib
import json
import math
import os
import random
import sqlite3
import struct
import subprocess
import time
from pathlib import Path
from typing import List, Optional

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- config

SKILLS_DIR = Path(os.environ.get("LIBRARIAN_SKILLS_DIR", "~/.claude/skills")).expanduser()
# when the same skill name exists at several paths, copies under these
# top-level prefixes win the dedupe race (comma-separated, in order)
CANONICAL_PREFIXES = [p.strip() for p in os.environ.get("LIBRARIAN_CANONICAL_PREFIXES", "").split(",") if p.strip()]
DB_PATH = Path(os.environ.get("LIBRARIAN_DB", "~/.skill_librarian/librarian.db")).expanduser()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("LIBRARIAN_EMBED_MODEL", "nomic-embed-text")

RERANK_BIN = os.environ.get("LIBRARIAN_RERANK_BIN", "")  # e.g. bin/afm-rerank; empty = off
RERANK_TIMEOUT = 30.0        # seconds; on timeout/error find falls back to embed order
# Soft filter: a local decision model marks each top candidate "likely" or
# "weak match" and find shows likely ones first. Nothing is dropped. On by the
# settings file, off with LIBRARIAN_SOFT_FILTER=off. Tested 2026-10-09
# (bench/decision-filter-2026-10-09): nimble lifted precision from 33% to 72%.
# nimble-v3 (bench/nimble-v3-2026-10-13) reached 80% and gives probabilities,
# so likely ones are also ordered by fit; on trial until FMD 2026-10-16.
SOFT_FILTER_CONFIG = Path(os.environ.get(
    "LIBRARIAN_SOFT_FILTER_CONFIG", "~/.skill_librarian/soft_filter.json")).expanduser()
SOFT_FILTER_LOG = DB_PATH.parent / "soft_filter.jsonl"

RECENCY_WINDOW = 10          # last N queries considered for the repeat penalty
RECENCY_PENALTY = 0.15       # score subtracted per recent appearance
MMR_LAMBDA_FIND = 0.7        # relevance-vs-diversity for find (higher = more relevant)
MMR_LAMBDA_BRAINSTORM = 0.45 # brainstorm leans diverse

# --- call counter: one JSONL line per tool call (ts, tool, client) ---------
import json as _json, os as _os
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path as _Path

_CALL_LOG = _Path(
    _os.environ.get("SKILL_LIBRARIAN_CALL_LOG")
    or _Path(_os.environ.get("XDG_STATE_HOME") or _Path.home() / ".local" / "state")
    / "skill-librarian-mcp" / "calls.jsonl"
)

async def _count_tool_calls(_ctx, _call_next):
    """mcp 2.x server middleware; counting must never break the server."""
    if _ctx.method == "tools/call":
        try:
            try:
                _cp = _ctx.session.client_params
                _info = getattr(_cp, "client_info", None) or getattr(_cp, "clientInfo")
                _client, _ver = _info.name, _info.version
            except Exception:
                _client, _ver = "unknown", None
            _CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _CALL_LOG.open("a", encoding="utf-8") as _f:
                _f.write(_json.dumps({
                    "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                    "tool": (_ctx.params or {}).get("name", "?"),
                    "client": _client,
                    "client_version": _ver,
                }) + "\n")
        except Exception:
            pass
    return await _call_next(_ctx)

mcp = MCPServer("skill_librarian_mcp", middleware=[_count_tool_calls])
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------- storage

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")  # without this, ON DELETE CASCADE never fires
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        path TEXT NOT NULL,
        description TEXT NOT NULL,
        body TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        indexed_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS embeddings (
        skill_id INTEGER PRIMARY KEY REFERENCES skills(id) ON DELETE CASCADE,
        dim INTEGER NOT NULL,
        vec BLOB NOT NULL
      );
      CREATE TABLE IF NOT EXISTS query_log (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        mode TEXT NOT NULL,
        query TEXT NOT NULL,
        returned TEXT NOT NULL          -- JSON list of skill names
      );
      CREATE TABLE IF NOT EXISTS outcome_log (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        skill_name TEXT NOT NULL,
        worked INTEGER NOT NULL,        -- 1 / 0
        note TEXT
      );
    """)
    return conn

def pack(v: List[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

def unpack(b: bytes, dim: int) -> List[float]:
    return list(struct.unpack(f"{dim}f", b))

# ---------------------------------------------------------------- embeddings

def embed(text: str) -> List[float]:
    """Embed text via Ollama. Raises with an actionable message on failure."""
    try:
        # /api/embed (not legacy /api/embeddings): truncates inputs that exceed
        # the model context instead of 500-ing — dense scripts (Korean etc.)
        # blow past the token limit at far fewer chars than English.
        r = httpx.post(
            f"{OLLAMA_HOST}/api/embed",
            json={"model": EMBED_MODEL, "input": text[:8000], "truncate": True},
            timeout=30.0,
        )
        r.raise_for_status()
        return r.json()["embeddings"][0]
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_HOST}. Is it running? "
            f"Set OLLAMA_HOST to a reachable node (e.g. a Tailscale IP). ({e})"
        )
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Ollama rejected the embed request ({e.response.status_code}). "
            f"Pull the model first: `ollama pull {EMBED_MODEL}`"
        )

def cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

# ---------------------------------------------------------------- indexing

def parse_skill(path: Path) -> Optional[dict]:
    """Parse a SKILL.md: frontmatter name/description + full body."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name, desc = path.parent.name, ""
    body_start = 0
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            body_start = end + 3
            lines = text[3:end].splitlines()
            i = 0
            while i < len(lines):
                line = lines[i]
                for key in ("name", "description"):
                    if line.startswith(key + ":"):
                        val = line[len(key) + 1:].strip()
                        if val in ("", "|", ">", "|-", ">-", "|+", ">+"):
                            # YAML block scalar OR plain multiline (value on the
                            # following indented lines): join the lines that follow
                            block = []
                            while i + 1 < len(lines) and (not lines[i + 1].strip() or lines[i + 1][:1] in (" ", "\t")):
                                i += 1
                                block.append(lines[i].strip())
                            val = " ".join(b for b in block if b)
                        val = val.strip("\"'")
                        if key == "name" and val:
                            name = val
                        elif key == "description" and val:
                            desc = val
                i += 1
    if not desc:
        # fall back to first real paragraph of the BODY (after the frontmatter),
        # de-blockquoted — never the frontmatter's own key: value lines
        for line in text[body_start:].splitlines():
            s = line.strip().lstrip(">").strip()
            if s and not s.startswith(("#", "---", "```")):
                desc = s
                break
    return {"name": name, "path": str(path), "description": desc, "body": text}

def iter_skill_files() -> List[Path]:
    """Walk SKILLS_DIR for SKILL.md files, following directory symlinks.

    Path.rglob doesn't follow symlinks on 3.12, which silently drops every
    symlinked skill dir. Dedupe by realpath so loops/aliases visit once.
    """
    seen, files = set(), []
    for root, dirs, names in os.walk(SKILLS_DIR, followlinks=True):
        real = os.path.realpath(root)
        if real in seen:
            dirs[:] = []  # already visited this physical dir via another link
            continue
        seen.add(real)
        if "SKILL.md" in names:
            files.append(Path(root) / "SKILL.md")

    def rank(p: Path):
        # (prefix order, depth, path): inside one prefix a shallower path wins, so a
        # personal override at kika-skills/<name> beats the same name nested in a
        # pack such as kika-skills/akakika-skills/skills/.../<name> (2026-09-12)
        rel = str(p.relative_to(SKILLS_DIR))
        depth = rel.count("/")
        for i, prefix in enumerate(CANONICAL_PREFIXES):
            if rel.startswith(prefix.rstrip("/") + "/"):
                return (i, depth, str(p))
        return (len(CANONICAL_PREFIXES), depth, str(p))

    return sorted(files, key=rank)

def reindex_collection() -> dict:
    """Scan SKILLS_DIR for SKILL.md files; embed new/changed; prune deleted."""
    conn = db()
    found, added, updated = [], 0, 0
    for path in iter_skill_files():
        s = parse_skill(path)
        if not s:
            continue
        if s["name"] in found:
            continue  # duplicate skill name at another path — first wins, avoids re-embed flip-flop
        found.append(s["name"])
        # hash the full searchable surface, not just the body — name/description
        # changes (or parser fixes) must trigger a re-embed too
        h = hashlib.sha256(f"{s['name']}\n{s['description']}\n{s['body']}".encode()).hexdigest()
        row = conn.execute(
            "SELECT id, body_hash FROM skills WHERE name=?", (s["name"],)
        ).fetchone()
        if row and row[1] == h:
            # same content, but the winning copy's location may have changed
            conn.execute("UPDATE skills SET path=? WHERE id=? AND path<>?", (s["path"], row[0], s["path"]))
            continue
        # embed name + description + first 4k of body — the searchable surface
        vec = embed(f"{s['name']}\n{s['description']}\n{s['body'][:4000]}")
        if row:
            conn.execute(
                "UPDATE skills SET path=?, description=?, body=?, body_hash=?, indexed_at=? WHERE id=?",
                (s["path"], s["description"], s["body"], h, time.time(), row[0]),
            )
            conn.execute(
                "UPDATE embeddings SET dim=?, vec=? WHERE skill_id=?",
                (len(vec), pack(vec), row[0]),
            )
            updated += 1
        else:
            cur = conn.execute(
                "INSERT INTO skills(name, path, description, body, body_hash, indexed_at) VALUES (?,?,?,?,?,?)",
                (s["name"], s["path"], s["description"], s["body"], h, time.time()),
            )
            conn.execute(
                "INSERT INTO embeddings(skill_id, dim, vec) VALUES (?,?,?)",
                (cur.lastrowid, len(vec), pack(vec)),
            )
            added += 1
        if (added + updated) % 50 == 0:
            conn.commit()  # don't lose a whole run to one bad embed
    # prune skills whose files vanished
    pruned = 0
    for (sid, name) in conn.execute("SELECT id, name FROM skills").fetchall():
        if name not in found:
            conn.execute("DELETE FROM skills WHERE id=?", (sid,))
            pruned += 1
    conn.commit()
    conn.close()
    return {"indexed": len(found), "added": added, "updated": updated, "pruned": pruned}

# ---------------------------------------------------------------- retrieval

def recent_appearance_counts(conn: sqlite3.Connection) -> dict:
    counts: dict = {}
    rows = conn.execute(
        "SELECT returned FROM query_log ORDER BY id DESC LIMIT ?", (RECENCY_WINDOW,)
    ).fetchall()
    for (returned,) in rows:
        for name in json.loads(returned):
            counts[name] = counts.get(name, 0) + 1
    return counts

def outcome_stats(conn: sqlite3.Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(worked),0) FROM outcome_log WHERE skill_name=?",
        (name,),
    ).fetchone()
    total, wins = row
    return {"uses": total, "success_rate": round(wins / total, 2) if total else None}

def mmr_select(query_vec, candidates, k, lam, recent_counts):
    """Maximal marginal relevance with a recency penalty. candidates: (name, desc, vec, sim)."""
    selected, pool = [], list(candidates)
    while pool and len(selected) < k:
        best, best_score = None, -1e9
        for c in pool:
            relevance = c[3] - RECENCY_PENALTY * recent_counts.get(c[0], 0)
            redundancy = max((cosine(c[2], s[2]) for s in selected), default=0.0)
            score = lam * relevance - (1 - lam) * redundancy
            if score > best_score:
                best, best_score = c, score
        selected.append(best)
        pool.remove(best)
    return selected

def retrieve(query: str, k: int, lam: float, wildcards: int = 0, log: bool = True) -> List[dict]:
    conn = db()
    rows = conn.execute(
        "SELECT s.name, s.description, e.dim, e.vec FROM skills s JOIN embeddings e ON e.skill_id=s.id"
    ).fetchall()
    if not rows:
        conn.close()
        raise RuntimeError(
            f"Index is empty. Run librarian_reindex first (skills dir: {SKILLS_DIR})."
        )
    qv = embed(query)
    cands = [(n, d, unpack(v, dim), cosine(qv, unpack(v, dim))) for n, d, dim, v in rows]
    cands.sort(key=lambda c: c[3], reverse=True)
    recent = recent_appearance_counts(conn)
    picked = mmr_select(qv, cands[: max(k * 4, 16)], k, lam, recent)

    if wildcards:
        chosen = {c[0] for c in picked}
        leftovers = [c for c in cands if c[0] not in chosen]
        picked += random.sample(leftovers, min(wildcards, len(leftovers)))

    results = []
    for name, desc, _vec, sim in picked:
        stats = outcome_stats(conn, name)
        results.append({
            "skill": name,
            "description": desc,
            "similarity": round(sim, 3),
            "recently_recommended": recent.get(name, 0),
            **stats,
        })
    if log:
        log_query(conn, "brainstorm" if wildcards else "find", query, results)
    conn.close()
    return results

def log_query(conn, mode: str, query: str, results: List[dict]) -> None:
    conn.execute(
        "INSERT INTO query_log(ts, mode, query, returned) VALUES (?,?,?,?)",
        (time.time(), mode, query, json.dumps([r["skill"] for r in results])),
    )
    conn.commit()

def afm_rerank(intent: str, results: List[dict]):
    """Opt-in deliberation pass: a local LLM (Apple Foundation Model via
    LIBRARIAN_RERANK_BIN) reorders the shortlist and adds why / why-not
    reasoning per candidate. Best-effort — any failure returns the embedding
    order untouched. The model's output is validated against the real
    candidate set; hallucinated or duplicate skill names are dropped."""
    if not RERANK_BIN or len(results) < 2:
        return results, None
    try:
        payload = json.dumps({"intent": intent, "candidates": [
            {"skill": r["skill"], "description": r["description"]} for r in results]})
        proc = subprocess.run(
            [RERANK_BIN], input=payload.encode(),
            capture_output=True, timeout=RERANK_TIMEOUT,
        )
        if proc.returncode != 0:
            return results, None
        data = json.loads(proc.stdout)
        by_name = {r["skill"]: r for r in results}
        merged, seen = [], set()
        for rk in data.get("rankings", []):
            n = rk.get("skill")
            if n in by_name and n not in seen:
                seen.add(n)
                merged.append({**by_name[n], "fit": rk.get("fit"),
                               "why": rk.get("why"), "why_not": rk.get("why_not")})
        merged += [r for r in results if r["skill"] not in seen]
        pick = data.get("pick")
        return merged, pick if pick in by_name else None
    except Exception:
        return results, None

def soft_filter_settings() -> Optional[dict]:
    """The soft filter's settings, or None when it is off."""
    if os.environ.get("LIBRARIAN_SOFT_FILTER", "").lower() in ("off", "0", "false", "no"):
        return None
    try:
        cfg = json.loads(SOFT_FILTER_CONFIG.read_text())
    except Exception:
        return None
    if not cfg.get("enabled"):
        return None
    return {
        "model": str(cfg.get("model", "nimble:latest")),
        "fallback_model": str(cfg.get("fallback_model") or ""),
        "likely_at": float(cfg.get("likely_at", 0.3)),
        "judge_top": max(2, int(cfg.get("judge_top", 5))),
        "timeout_s": float(cfg.get("timeout_s", 5.0)),
    }

def _is_decision_api(model: str) -> bool:
    """nimble-v3 and laya answer on Ollama's /v1/systemone with probabilities;
    the old nimble answers a text prompt with A or B."""
    return model.startswith(("nimble-v3", "laya"))

FIT_PROMPT = """Schema:
field: fit
meaning: Does the skill help accomplish the user's intent? Judge only from the skill description.
choices:
A: yes, the skill is directly useful for this task
B: no, the skill is unrelated, too generic, or for a different task

Context:
Intent: {intent}
Skill: {skill} - {description}

Field: fit
Answer:"""

async def _judge_fit(client: httpx.AsyncClient, model: str, intent: str, r: dict) -> bool:
    resp = await client.post(f"{OLLAMA_HOST}/api/generate", json={
        "model": model, "stream": False, "think": False, "keep_alive": "30m",
        "prompt": FIT_PROMPT.format(intent=intent[:1000], skill=r["skill"],
                                    description=(r.get("description") or "")[:400]),
        "options": {"num_predict": 3, "temperature": 0},
    })
    resp.raise_for_status()
    return resp.json().get("response", "").strip().upper().startswith("A")

FIT_QUESTION = "Does this skill help accomplish the user's intent? Judge only from the skill description."

async def _judge_fit_prob(client: httpx.AsyncClient, model: str, intent: str, r: dict) -> float:
    """Probability (0 to 1) that the skill fits, from the decision API."""
    resp = await client.post(f"{OLLAMA_HOST}/v1/systemone", json={
        "model": model, "keep_alive": "30m",
        "state": f"Intent: {intent[:1000]}\nSkill: {r['skill']} - {(r.get('description') or '')[:600]}",
        "questions": {"fit": {"type": "noul", "instructions": FIT_QUESTION, "criteria": {
            "true": "The skill is directly useful for this task.",
            "false": "The skill is unrelated, too generic, or for a different task."}}},
    })
    resp.raise_for_status()
    return float(resp.json()["answers"]["fit"]["noul"])

async def _judge_all(model: str, intent: str, judged: List[dict], timeout_s: float) -> list:
    """One answer per candidate (a probability, a bool, or None when it failed)."""
    judge = _judge_fit_prob if _is_decision_api(model) else _judge_fit
    out: list = [None] * len(judged)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            tasks = [asyncio.create_task(judge(client, model, intent, r)) for r in judged]
            done, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for t in pending:
                t.cancel()
            for i, t in enumerate(tasks):
                if t in done and not t.cancelled() and t.exception() is None:
                    out[i] = t.result()
    except Exception:
        pass
    return out

async def soft_filter(intent: str, candidates: List[dict], k: int):
    """Mark the top candidates "likely" or "weak match" and put likely ones
    first. Nothing is dropped: weak matches stay in the list, below. With a
    decision-API model, each group is also ordered by the model's probability.
    When the main model gives nothing, the fallback model is tried. Any
    failure or a slow model returns the embedding order untouched."""
    cfg = soft_filter_settings()
    if not cfg or len(candidates) < 2:
        return candidates[:k], None
    judged = candidates[: cfg["judge_top"]]
    t0 = time.monotonic()
    model = cfg["model"]
    answers = await _judge_all(model, intent, judged, cfg["timeout_s"])
    fell_back = False
    if all(a is None for a in answers) and cfg["fallback_model"]:
        fell_back, model = True, cfg["fallback_model"]
        answers = await _judge_all(model, intent, judged, cfg["timeout_s"])
    probs = [a if isinstance(a, float) else None for a in answers]
    verdicts: List[Optional[bool]] = [
        None if a is None else (a >= cfg["likely_at"] if isinstance(a, float) else a) for a in answers]
    ms = round((time.monotonic() - t0) * 1000)
    meta = {"model": model, "ms": ms}
    if fell_back:
        meta["fallback"] = True
    if any(p is not None for p in probs):
        meta["probs"] = [None if p is None else round(p, 3) for p in probs]
    if all(v is None for v in verdicts):
        meta["status"] = "skipped (model slow or down)"
        _log_soft_filter(intent, judged, verdicts, meta)
        return candidates[:k], meta
    marked = []
    for r, v, p in zip(judged, verdicts, probs):
        match = {True: "likely", False: "weak match", None: "not judged"}[v]
        marked.append({**r, "match": match, **({"fit_prob": round(p, 2)} if p is not None else {})})
    by_prob = lambda r: -r.get("fit_prob", 0.0)  # stable: ties keep embedding order
    likely = sorted([r for r in marked if r["match"] == "likely"], key=by_prob)
    unjudged = [r for r in marked if r["match"] == "not judged"]
    weak = sorted([r for r in marked if r["match"] == "weak match"], key=by_prob)
    ordered = likely + unjudged + weak + candidates[len(judged):]
    meta["status"] = "ok"
    meta["likely"] = [r["skill"] for r in likely]
    meta["weak"] = [r["skill"] for r in weak]
    _log_soft_filter(intent, judged, verdicts, meta)
    return ordered[:k], meta

def _log_soft_filter(intent: str, judged: List[dict], verdicts, meta: dict) -> None:
    """One line per find, for the keep-or-drop review (weekly maintenance)."""
    try:
        with SOFT_FILTER_LOG.open("a") as f:
            f.write(json.dumps({
                "ts": time.time(), "intent": intent[:300],
                "order_in": [r["skill"] for r in judged],
                "apple_fit": [r.get("fit") for r in judged],
                "verdicts": verdicts, **meta,
            }) + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------- tool inputs

class FindInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    intent: str = Field(..., min_length=3, max_length=2000,
                        description="What you're trying to do right now, in plain language (e.g. 'package a python mcp server for distribution').")
    k: int = Field(default=3, ge=1, le=8, description="How many recommendations to return.")

class BrainstormInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    context: str = Field(..., min_length=3, max_length=4000,
                         description="The situation or problem space to riff on. Looser than an intent — describe the project state.")
    k: int = Field(default=6, ge=3, le=10, description="How many diverse candidates to surface.")

class ReportInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    skill: str = Field(..., min_length=1, max_length=200, description="Exact skill name as returned by librarian_find.")
    worked: bool = Field(..., description="Did the skill actually help complete the task?")
    note: Optional[str] = Field(default=None, max_length=1000, description="One line on why it worked or failed.")

# ---------------------------------------------------------------- tools

@mcp.tool(
    name="librarian_find",
    annotations={"title": "Find Skills for Intent", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def librarian_find(params: FindInput) -> str:
    """Recommend the best skills in the collection for a stated intent.

    Returns a ranked, diversity-adjusted shortlist with similarity scores,
    recent-recommendation counts, and historical success rates. Skills that were
    recommended in the last few queries are deliberately downweighted to avoid
    monotone suggestions. Logs the query for collection analytics.
    """
    cfg = soft_filter_settings()
    pool = max(params.k, cfg["judge_top"]) if cfg else params.k
    results = retrieve(params.intent, pool, MMR_LAMBDA_FIND, log=False)
    results, pick = afm_rerank(params.intent, results)
    results, judged = await soft_filter(params.intent, results, params.k)
    conn = db()
    log_query(conn, "find", params.intent, results)
    conn.close()
    out = {"intent": params.intent, "recommendations": results}
    if judged:
        out["soft_filter"] = judged
        if judged.get("status") == "ok":
            out["hint"] = 'Skills with match "likely" come first; "weak match" ones a local model doubted, still worth a glance.'
    if pick and pick in {r["skill"] for r in results}:
        out["pick"] = pick
    return json.dumps(out, indent=2)

@mcp.tool(
    name="librarian_brainstorm",
    annotations={"title": "Brainstorm Across the Collection", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def librarian_brainstorm(params: BrainstormInput) -> str:
    """Surface a wide, diverse spread of skills for ideation — not convergence.

    Uses a diversity-heavy selection plus 2 random wildcards from outside the
    relevant set, so unexpected skills get a chance to spark ideas. For each
    candidate, consider both why it fits and why it might not.
    """
    results = retrieve(params.context, params.k, MMR_LAMBDA_BRAINSTORM, wildcards=2)
    return json.dumps({
        "context": params.context,
        "candidates": results,
        "hint": "Last 2 entries are random wildcards. Argue for AND against each candidate before picking.",
    }, indent=2)

@mcp.tool(
    name="librarian_report",
    annotations={"title": "Report Skill Outcome", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def librarian_report(params: ReportInput) -> str:
    """Report whether a recommended skill actually worked. Append-only.

    This feeds success rates shown in future recommendations and the
    kill/keep digest in librarian_stats. Agents can only report outcomes —
    they cannot modify skills or rankings directly.
    """
    conn = db()
    exists = conn.execute("SELECT 1 FROM skills WHERE name=?", (params.skill,)).fetchone()
    conn.execute(
        "INSERT INTO outcome_log(ts, skill_name, worked, note) VALUES (?,?,?,?)",
        (time.time(), params.skill, int(params.worked), params.note),
    )
    conn.commit()
    conn.close()
    warn = "" if exists else f" (warning: '{params.skill}' is not in the index — name may be wrong)"
    return f"Logged: {params.skill} → {'worked' if params.worked else 'failed'}{warn}"

@mcp.tool(
    name="librarian_reindex",
    annotations={"title": "Reindex Skill Collection", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def librarian_reindex() -> str:
    """Rescan the skills directory, embed new/changed SKILL.md files, prune deleted ones.

    Run after adding or editing skills. Only changed files are re-embedded
    (content-hash check), so repeated runs are cheap.
    """
    stats = reindex_collection()
    return json.dumps({"skills_dir": str(SKILLS_DIR), **stats}, indent=2)

@mcp.tool(
    name="librarian_stats",
    annotations={"title": "Collection Health Digest", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def librarian_stats() -> str:
    """Digest of collection health for curation decisions.

    Shows: most-recommended skills, never-surfaced skills (kill candidates,
    plus the 100 oldest never-recommended ones for the weekly review),
    skills with poor success rates (rewrite candidates), and recent queries
    that matched nothing well (gaps — skills you should build).
    """
    conn = db()
    all_skills = [r[0] for r in conn.execute("SELECT name FROM skills").fetchall()]
    appearance: dict = {}
    for (q, returned) in conn.execute("SELECT query, returned FROM query_log ORDER BY id DESC LIMIT 200"):
        names = json.loads(returned)
        for n in names:
            appearance[n] = appearance.get(n, 0) + 1
    # success rates
    failing = []
    for name in all_skills:
        s = outcome_stats(conn, name)
        if s["uses"] and s["uses"] >= 3 and (s["success_rate"] or 0) < 0.5:
            failing.append({"skill": name, **s})
    # "never" means over the whole query log, not just the recent window
    ever: set = set()
    for (returned,) in conn.execute("SELECT returned FROM query_log"):
        ever.update(json.loads(returned))
    never_surfaced = sorted(n for n in all_skills if n not in ever)
    # Oldest first by SKILL.md file date, for the weekly unused-skills review
    oldest = []
    for name, path in conn.execute("SELECT name, path FROM skills"):
        if name in ever:
            continue
        try:
            st = Path(path).parent.stat()
        except OSError:
            continue
        # When the skill folder landed on this Mac. Unzipped packs keep fake 1979
        # dates in mtime and birth time; ctime is the real arrival then.
        when = getattr(st, "st_birthtime", st.st_mtime)
        if when < 1577836800:  # before 2020
            when = st.st_ctime
        oldest.append((when, name, path))
    oldest.sort()
    hot = sorted(appearance.items(), key=lambda kv: -kv[1])[:10]
    conn.close()
    # Digest, not a dump: with a young query_log most of the collection has
    # never surfaced — listing all ~3k names blows past tool-response limits.
    step = max(1, len(never_surfaced) // 20)
    return json.dumps({
        "total_skills": len(all_skills),
        "hot_skills": [{"skill": n, "recommended": c} for n, c in hot],
        "never_surfaced (kill candidates)": {
            "count": len(never_surfaced),
            "sample": never_surfaced[::step][:20],
            "full_list": "sqlite3 ~/.skill_librarian/librarian.db — skills not named in query_log.returned",
        },
        # Never recommended is not useless: the librarian only knows what agents asked for.
        "never_recommended_oldest_first": [
            {"skill": n, "file_date": time.strftime("%Y-%m-%d", time.localtime(m)),
             "path": _rel_to_collection(p)}
            for m, n, p in oldest[:100]
        ],
        "low_success_rate (rewrite candidates)": failing,
    }, indent=2)

def _rel_to_collection(path: str) -> str:
    folder = Path(path).resolve().parent
    root = SKILLS_DIR.resolve()
    return str(folder.relative_to(root)) if folder.is_relative_to(root) else str(folder)

# ---------------------------------------------------------------- entry

if __name__ == "__main__":
    mcp.run(transport="stdio")
