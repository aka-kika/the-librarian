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
        rel = str(p.relative_to(SKILLS_DIR))
        for i, prefix in enumerate(CANONICAL_PREFIXES):
            if rel.startswith(prefix.rstrip("/") + "/"):
                return (i, str(p))
        return (len(CANONICAL_PREFIXES), str(p))

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

def retrieve(query: str, k: int, lam: float, wildcards: int = 0) -> List[dict]:
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
    conn.execute(
        "INSERT INTO query_log(ts, mode, query, returned) VALUES (?,?,?,?)",
        (time.time(), "brainstorm" if wildcards else "find", query,
         json.dumps([r["skill"] for r in results])),
    )
    conn.commit()
    conn.close()
    return results

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
    results = retrieve(params.intent, params.k, MMR_LAMBDA_FIND)
    results, pick = afm_rerank(params.intent, results)
    out = {"intent": params.intent, "recommendations": results}
    if pick:
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

    Shows: most-recommended skills, never-surfaced skills (kill candidates),
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
    never_surfaced = sorted(n for n in all_skills if n not in appearance)
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
        "low_success_rate (rewrite candidates)": failing,
    }, indent=2)

# ---------------------------------------------------------------- entry

if __name__ == "__main__":
    mcp.run(transport="stdio")
