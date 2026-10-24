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
  librarian_stats     — usage digest: hot skills, dead skills, rewrite candidates

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
import re
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
# The pre-archive room: skills on a trial week out of the collection. Indexed
# into their own table, so find, brainstorm and get never hand them out; a
# search with no fit is matched against them, so a missed one can come back.
ROOM_DIR = Path(os.environ.get("LIBRARIAN_ROOM_DIR") or SKILLS_DIR.parent / "Skills-pre-archive").expanduser()
ROOM_MATCH_AT = 0.6          # a no-fit search names a room skill from this similarity
TRIAL_DAYS = 14              # stats: fetched skills judged over this many days
NO_FIT_DAYS = 7              # stats: no-fit searches looked at over this many days
FEW_SEARCHES = 20            # fewer searches than this in NO_FIT_DAYS: give the room a week more
TEST_CLIENTS = ("mcp",)      # smoke tests connect as the Python SDK's default client name
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
# find's "suggest" line names one skill only above this judge probability.
# "likely" starts at 0.3, too loose to put in front of Kika: on 2026-10-24 a
# cleanup intent got preparing-repo-for-public-release as likely at 0.64.
SUGGEST_AT = 0.7

RECENCY_WINDOW = 10          # last N queries considered for the repeat penalty
RECENCY_PENALTY = 0.15       # score subtracted per recent appearance
# The closest match of each search skips the repeat penalty (Kika, 2026-10-24).
# The window is shared by every agent and the top hits sit within ~0.1 of each
# other, so one repeat (0.15) pushed the right skill out of its own job (wrap
# at 0.65 for "wrap up the session"). The penalty still breaks near-ties below.
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

_CLIENT = "unknown"  # one stdio process serves one agent, so the last caller is the caller

async def _count_tool_calls(_ctx, _call_next):
    """mcp 2.x server middleware; counting must never break the server."""
    global _CLIENT
    if _ctx.method == "tools/call":
        try:
            try:
                _cp = _ctx.session.client_params
                _info = getattr(_cp, "client_info", None) or getattr(_cp, "clientInfo")
                _client, _ver = _info.name, _info.version
                _CLIENT = _client
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

# Sent to every agent when it connects: this is how agents notice the librarian
# at all (Claude, Grok, aka, Goose, Cursor and Hermes all read it).
INSTRUCTIONS = """\
The skill librarian recommends skills from Kika's collection: step-by-step know-how for a job.
- If your agent already has a skill for the job (Claude Code loads Kika's own skills, like wrap or goodnight), use that one. Ask the librarian only for what you do not have.
- Before a real task (not a small edit or a quick question), call librarian_find with what you are about to do, in plain words.
- Its "suggest" line names a skill only when one truly fits; "No skill fits, carry on" means do the task without one. Tell the user in one line which skill you use and why.
- To read a skill your agent does not have, call librarian_get(skill): it hands over the whole skill. Never copy skill files into your agent's own folders.
- After using one, call librarian_report(skill, worked, note). If it was offered for the wrong job, report worked=false and say so in the note.
- Agents read recommendations and write only outcomes: never edit skills or the librarian's data."""

mcp = MCPServer("skill_librarian_mcp", instructions=INSTRUCTIONS, middleware=[_count_tool_calls])
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------- storage

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
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
      CREATE TABLE IF NOT EXISTS fetch_log (  -- append-only, like the logs above
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        skill_name TEXT NOT NULL,
        client TEXT                     -- the agent that fetched it
      );
      CREATE TABLE IF NOT EXISTS suggest_log (  -- append-only: one row per find
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        intent TEXT NOT NULL,
        client TEXT,
        suggested TEXT,                 -- the skill named, NULL for "no skill fits"
        room_match TEXT,                -- closest room skill when nothing fit
        room_sim REAL
      );
      CREATE TABLE IF NOT EXISTS room_skills (  -- index of ROOM_DIR, never handed out
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        path TEXT NOT NULL,
        description TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vec BLOB NOT NULL,
        indexed_at REAL NOT NULL
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
        embeddings = r.json().get("embeddings")
        if not embeddings or not embeddings[0]:
            raise RuntimeError(
                f"Ollama rejected the embed request (empty embeddings). "
                f"Pull the model first: `ollama pull {EMBED_MODEL}`"
            )
        return embeddings[0]
    except httpx.RequestError as e:
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
    if len(a) != len(b):
        return 0.0
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
        m = re.search(r"^---[ \t\r]*$", text[3:], re.M)
        if m:
            body_start = 3 + m.end()
            lines = text[3 : 3 + m.start()].splitlines()
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
                        else:
                            while i + 1 < len(lines) and lines[i + 1][:1] in (" ", "\t"):
                                i += 1
                                if lines[i].strip():
                                    val += " " + lines[i].strip()
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
                desc = s[:500]
                break
    return {"name": name, "path": str(path), "description": desc, "body": text}

def iter_skill_files() -> List[Path]:
    """Walk SKILLS_DIR for SKILL.md files, following directory symlinks.

    Path.rglob doesn't follow symlinks on 3.12, which silently drops every
    symlinked skill dir. Every alias path is collected and ranked first; only
    then are aliases of the same physical file dropped, so the canonical path
    wins whichever alias os.walk reached first (2026-10-14). A link back to one
    of its own parent folders is not followed, so loops end.
    """
    files, chain = [], {}
    for root, dirs, names in os.walk(SKILLS_DIR, followlinks=True):
        # physical dirs on the path from SKILLS_DIR down to here
        here = chain.get(os.path.dirname(root), frozenset()) | {os.path.realpath(root)}
        chain[root] = here
        dirs[:] = [d for d in dirs if os.path.realpath(os.path.join(root, d)) not in here]
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

    out, seen = [], set()
    for p in sorted(files, key=rank):
        real = os.path.realpath(p)
        if real not in seen:  # an alias of a file already kept at a better path
            seen.add(real)
            out.append(p)
    return out

def reindex_collection(force: bool = False) -> dict:
    """Scan SKILLS_DIR for SKILL.md files; embed new/changed; prune deleted."""
    if not SKILLS_DIR.is_dir():
        return {"error": f"Skills folder missing or not a directory: {SKILLS_DIR}"}
    skills, found = [], []
    for path in iter_skill_files():
        s = parse_skill(path)
        if not s:
            continue
        if s["name"] in found:
            continue  # duplicate skill name at another path — first wins, avoids re-embed flip-flop
        found.append(s["name"])
        skills.append(s)
    if not skills:
        return {"error": f"Found 0 skills in {SKILLS_DIR}; refusing to prune index."}
    conn = db()
    try:
        existing = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        if not force and len(skills) < existing / 2:
            return {
                "error": f"Found {len(skills)} skills vs {existing} indexed (<50%); refusing to prune (pass force=True to allow)."
            }
        added, updated = 0, 0
        for s in skills:
            # hash the full searchable surface, not just the body — name/description
            # changes (or parser fixes) must trigger a re-embed too
            h = hashlib.sha256(f"{EMBED_MODEL}\n{s['name']}\n{s['description']}\n{s['body']}".encode()).hexdigest()
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
        return {"indexed": len(found), "added": added, "updated": updated, "pruned": pruned,
                "room": reindex_room(conn)}
    finally:
        conn.close()

def reindex_room(conn: sqlite3.Connection) -> dict:
    """Mirror ROOM_DIR into room_skills: embed new/changed, drop what left the room.
    A missing room is not an error: it just means no skill is on trial out."""
    skills, found = [], set()
    if ROOM_DIR.is_dir():
        for root, _dirs, names in os.walk(ROOM_DIR):
            if "SKILL.md" in names:
                s = parse_skill(Path(root) / "SKILL.md")
                if s and s["name"] not in found:
                    found.add(s["name"])
                    skills.append(s)
    added = 0
    for s in skills:
        h = hashlib.sha256(f"{EMBED_MODEL}\n{s['name']}\n{s['description']}\n{s['body']}".encode()).hexdigest()
        row = conn.execute("SELECT body_hash FROM room_skills WHERE name=?", (s["name"],)).fetchone()
        if row and row[0] == h:
            continue
        vec = embed(f"{s['name']}\n{s['description']}\n{s['body'][:4000]}")  # same surface as the collection
        conn.execute("DELETE FROM room_skills WHERE name=?", (s["name"],))
        conn.execute(
            "INSERT INTO room_skills(name, path, description, body_hash, dim, vec, indexed_at) VALUES (?,?,?,?,?,?,?)",
            (s["name"], s["path"], s["description"], h, len(vec), pack(vec), time.time()))
        added += 1
    left = 0
    for (name,) in conn.execute("SELECT name FROM room_skills").fetchall():
        if name not in found:
            conn.execute("DELETE FROM room_skills WHERE name=?", (name,))
            left += 1
    conn.commit()
    return {"dir": str(ROOM_DIR), "in_room": len(found), "added": added, "left": left}

def closest_room_skill(intent: str) -> Optional[tuple]:
    """(name, similarity) of the room skill nearest the intent, or None."""
    conn = db()
    try:
        rows = conn.execute("SELECT name, dim, vec FROM room_skills").fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    qv = embed(intent)
    return max(((n, cosine(qv, unpack(v, d))) for n, d, v in rows), key=lambda x: x[1])

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
    closest = max(pool, key=lambda c: c[3])[0] if pool else None
    while pool and len(selected) < k:
        best, best_score = None, -1e9
        for c in pool:
            repeats = 0 if c[0] == closest else recent_counts.get(c[0], 0)
            relevance = c[3] - RECENCY_PENALTY * repeats
            redundancy = max((cosine(c[2], s[2]) for s in selected), default=0.0)
            score = lam * relevance - (1 - lam) * redundancy
            if score > best_score:
                best, best_score = c, score
        selected.append(best)
        pool.remove(best)
    return selected

def retrieve(query: str, k: int, lam: float, wildcards: int = 0, log: bool = True) -> List[dict]:
    conn = db()
    try:
        rows = conn.execute(
            "SELECT s.name, s.description, e.dim, e.vec FROM skills s JOIN embeddings e ON e.skill_id=s.id"
        ).fetchall()
        if not rows:
            raise RuntimeError(
                f"Index is empty. Run librarian_reindex first (skills dir: {SKILLS_DIR})."
            )
        qv = embed(query)
        cands = [(n, d, unpack(v, dim), cosine(qv, unpack(v, dim))) for n, d, dim, v in rows]
        cands.sort(key=lambda c: c[3], reverse=True)
        recent = recent_appearance_counts(conn)
        # wildcards count toward k, so a brainstorm returns k items in all
        picked = mmr_select(qv, cands[: max(k * 4, 16)], max(1, k - wildcards), lam, recent)

        wild = []
        if wildcards:
            chosen = {c[0] for c in picked}
            leftovers = [c for c in cands if c[0] not in chosen]
            wild = random.sample(leftovers, min(wildcards, len(leftovers)))

        results = []
        for name, desc, _vec, sim in picked + wild:
            stats = outcome_stats(conn, name)
            results.append({
                "skill": name,
                "description": desc,
                "similarity": round(sim, 3),
                "recently_recommended": recent.get(name, 0),
                **stats,
            })
        if log:
            # wildcards are random, not recommendations: logging them would put
            # the recency penalty on random skills for the next queries
            log_query(conn, "brainstorm" if wildcards else "find", query, results[: len(picked)])
        return results
    finally:
        conn.close()

def log_query(conn, mode: str, query: str, results: List[dict]) -> None:
    conn.execute(
        "INSERT INTO query_log(ts, mode, query, returned) VALUES (?,?,?,?)",
        (time.time(), mode, query, json.dumps([r["skill"] for r in results])),
    )
    conn.commit()

def _log_find(intent: str, results: List[dict]) -> None:
    conn = db()
    try:
        log_query(conn, "find", intent, results)
    finally:
        conn.close()

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

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")

def _one_line(text: str) -> str:
    """Skill names and descriptions come from third-party SKILL.md files: no line
    breaks or control characters, so one cannot fake a new prompt line such as
    "Answer: A". A no-op on the 2026-10-14 index; the tag fence the reranker
    got waits for the nimble-v3 verdict (FMD 2026-10-16)."""
    return _CONTROL.sub(" ", text)

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
        "prompt": FIT_PROMPT.format(intent=intent[:1000], skill=_one_line(r["skill"]),
                                    description=_one_line((r.get("description") or "")[:400])),
        "options": {"num_predict": 3, "temperature": 0},
    })
    resp.raise_for_status()
    return resp.json().get("response", "").strip().upper().startswith("A")

FIT_QUESTION = "Does this skill help accomplish the user's intent? Judge only from the skill description."

async def _judge_fit_prob(client: httpx.AsyncClient, model: str, intent: str, r: dict) -> float:
    """Probability (0 to 1) that the skill fits, from the decision API."""
    resp = await client.post(f"{OLLAMA_HOST}/v1/systemone", json={
        "model": model, "keep_alive": "30m",
        "state": f"Intent: {intent[:1000]}\nSkill: {_one_line(r['skill'])} - {_one_line((r.get('description') or '')[:600])}",
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
            await asyncio.gather(*pending, return_exceptions=True)
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
    k: int = Field(default=6, ge=3, le=10, description="How many diverse candidates to surface, the 2 random wildcards included.")

class GetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    skill: str = Field(..., min_length=1, max_length=200, description="Exact skill name as returned by librarian_find.")

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
    # blocking work (HTTP, subprocess, SQLite) runs in a thread so one slow
    # search never freezes the server's event loop for other calls
    results = await asyncio.to_thread(retrieve, params.intent, pool, MMR_LAMBDA_FIND, log=False)
    results, pick = await asyncio.to_thread(afm_rerank, params.intent, results)
    results, judged = await soft_filter(params.intent, results, params.k)
    await asyncio.to_thread(_log_find, params.intent, results)
    out = {"intent": params.intent, "recommendations": results}
    if judged:
        out["soft_filter"] = judged
        if judged.get("status") == "ok":
            out["hint"] = 'Skills with match "likely" come first; "weak match" ones a local model doubted, still worth a glance.'
    if pick and pick in {r["skill"] for r in results}:
        out["pick"] = pick
    out["suggest"], named = _suggestion(results, judged, out.get("pick"))
    await asyncio.to_thread(_log_suggestion, params.intent, named)
    return json.dumps(out, indent=2)

def _log_suggestion(intent: str, named: Optional[str]) -> None:
    """One suggest_log row per find. When nothing fit, also the closest room
    skill: the room's trial week is judged on these. Never breaks a find."""
    try:
        room = None if named else closest_room_skill(intent)
        conn = db()
        try:
            conn.execute(
                "INSERT INTO suggest_log(ts, intent, client, suggested, room_match, room_sim) VALUES (?,?,?,?,?,?)",
                (time.time(), intent, _CLIENT, named, room[0] if room else None,
                 round(room[1], 3) if room else None))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

def _suggestion(results: List[dict], judged: Optional[dict], pick: Optional[str]) -> tuple:
    """(line, skill): one plain line the agent can pass on, naming a skill only
    when the judge is sure enough, otherwise "carry on" so a weak list makes no
    noise; skill is None then."""
    if judged and judged.get("status") == "ok":
        sure = [r for r in results if r.get("match") == "likely"
                and r.get("fit_prob", 1.0) >= SUGGEST_AT]  # yes/no judges give no prob
        sure.sort(key=lambda r: r["skill"] != pick)  # the reranker's pick first, if sure
        best = sure[0] if sure else None
    else:
        # judge off or down: fall back to the reranker's own fit score
        best = next((r for r in results if r["skill"] == pick and (r.get("fit") or 0) >= 8), None)
    if not best:
        return "No skill fits, carry on.", None
    why = f" Why: {best['why']}" if best.get("why") else ""
    return f"Skill that fits: {best['skill']}.{why}", best["skill"]

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
    results = await asyncio.to_thread(retrieve, params.context, params.k, MMR_LAMBDA_BRAINSTORM, wildcards=2)
    return json.dumps({
        "context": params.context,
        "candidates": results,
        "hint": "Last 2 entries are random wildcards. Argue for AND against each candidate before picking.",
    }, indent=2)

GET_MAX_CHARS = 60_000   # one SKILL.md; longer ones point at the file for the rest
GET_MAX_FILES = 40       # other files listed from the skill's folder

@mcp.tool(
    name="librarian_get",
    annotations={"title": "Get a Skill", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def librarian_get(params: GetInput) -> str:
    """Hand over one skill in full: its SKILL.md and a list of the other files in its folder.

    For a skill librarian_find recommended that your agent does not already
    have. Each fetch is logged (append-only) so the skill's trial can be judged.
    """
    return await asyncio.to_thread(_get, params)

def _get(params: GetInput) -> str:
    conn = db()
    try:
        row = conn.execute("SELECT name, path, body FROM skills WHERE name=?", (params.skill,)).fetchone()
        if not row and conn.execute("SELECT 1 FROM room_skills WHERE name=?", (params.skill,)).fetchone():
            return json.dumps({"error": f"'{params.skill}' is in the pre-archive room (a trial week out "
                                        "of the collection): not handed out. Do the task without it."})
        if not row:
            like = [n for (n,) in conn.execute(
                "SELECT name FROM skills WHERE name LIKE ? ORDER BY length(name) LIMIT 5",
                (f"%{params.skill}%",))]
            return json.dumps({"error": f"No skill named '{params.skill}' in the index.",
                               "did_you_mean": like})
        name, path, body = row
        skill_md = Path(path)
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = body  # moved since the last reindex: hand over the indexed copy
        conn.execute("INSERT INTO fetch_log(ts, skill_name, client) VALUES (?,?,?)",
                     (time.time(), name, _CLIENT))
        conn.commit()
    finally:
        conn.close()
    folder = skill_md.parent
    others = []
    for root, dirs, files in os.walk(folder):  # stops early: a skill folder can hold a whole repo
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "node_modules")))
        for fn in sorted(files):
            rel = Path(root, fn).relative_to(folder)
            if not fn.startswith(".") and str(rel) != "SKILL.md":
                others.append(str(rel))
        if len(others) >= GET_MAX_FILES:
            others = others[:GET_MAX_FILES]
            break
    out = {"skill": name, "folder": str(folder), "skill_md": text[:GET_MAX_CHARS]}
    if len(text) > GET_MAX_CHARS:
        out["truncated"] = f"Only the first {GET_MAX_CHARS} characters; read the rest at {skill_md}"
    if others:
        out["other_files"] = others  # relative to folder; read the ones SKILL.md points to
    out["next"] = "Follow the skill, then call librarian_report(skill, worked, note)."
    return json.dumps(out, indent=2)

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
    return await asyncio.to_thread(_report, params)

def _report(params: ReportInput) -> str:
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
async def librarian_reindex(force: bool = False) -> str:
    """Rescan the skills directory, embed new/changed SKILL.md files, prune deleted ones.

    Run after adding or editing skills. Only changed files are re-embedded
    (content-hash check), so repeated runs are cheap.
    """
    stats = await asyncio.to_thread(reindex_collection, force=force)
    return json.dumps({"skills_dir": str(SKILLS_DIR), **stats}, indent=2)

@mcp.tool(
    name="librarian_stats",
    annotations={"title": "Collection Health Digest", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def librarian_stats() -> str:
    """Digest of collection health for curation decisions.

    Shows: most-recommended skills, never-surfaced skills (kill candidates,
    plus the 100 oldest never-recommended ones for the weekly review), and
    skills with poor success rates (rewrite candidates).
    """
    return await asyncio.to_thread(_stats)

def _stats() -> str:
    conn = db()
    all_skills = [r[0] for r in conn.execute("SELECT name FROM skills").fetchall()]
    appearance: dict = {}
    for (returned,) in conn.execute("SELECT returned FROM query_log ORDER BY id DESC LIMIT 200"):
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
    on_trial, no_fit = _on_trial(conn), _no_fit(conn)
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
        "on_trial (fetched with librarian_get)": on_trial,
        "no_skill_fit (the room's trial)": no_fit,
    }, indent=2)

# A failed report whose note says the skill was offered for the wrong job is
# a search problem, not a bad skill: it never counts against the skill.
WRONG_JOB = re.compile(r"wrong (skill|job|fit)|rejected|unrelated|not a fit|doesn'?t (fit|match)|wrong shape", re.I)

def _not_test(column: str = "client") -> str:
    return f"COALESCE({column},'') NOT IN ({','.join('?' * len(TEST_CLIENTS))})"

def _on_trial(conn: sqlite3.Connection) -> list:
    """Skills fetched in the last TRIAL_DAYS, judged on the reports since their
    first fetch. Only flags for Kika; nothing is moved."""
    since = time.time() - TRIAL_DAYS * 86400
    out = []
    for name, fetches, first in conn.execute(
            f"SELECT skill_name, COUNT(*), MIN(ts) FROM fetch_log WHERE ts>=? AND {_not_test()} "
            "GROUP BY skill_name ORDER BY COUNT(*) DESC", (since, *TEST_CLIENTS)).fetchall():
        reports = conn.execute("SELECT worked, note FROM outcome_log WHERE skill_name=? AND ts>=?",
                               (name, first)).fetchall()
        wrong = sum(1 for w, n in reports if not w and n and WRONG_JOB.search(n))
        real = len(reports) - wrong
        worked = sum(w for w, _ in reports)
        if wrong >= 2:
            verdict = "flag: offered for the wrong job, sharpen its description"
        elif real < 3:
            verdict = "too few reports yet"
        elif worked < real * 2 / 3:
            verdict = f"flag for Kika: {worked} of {real} worked"
        else:
            verdict = f"fine: {worked} of {real} worked"
        out.append({"skill": name, "fetches": fetches, "reports": len(reports),
                    "worked": worked, "wrong_job": wrong, "verdict": verdict})
    return out

def _no_fit(conn: sqlite3.Connection) -> dict:
    """Searches in the last NO_FIT_DAYS where no skill fit, and the room skills
    they came close to: the comeback candidates for the room's trial week."""
    since = time.time() - NO_FIT_DAYS * 86400
    rows = conn.execute(
        f"SELECT intent, suggested, room_match, room_sim FROM suggest_log WHERE ts>=? AND {_not_test()} ORDER BY id",
        (since, *TEST_CLIENTS)).fetchall()
    misses = [r for r in rows if r[1] is None]
    near: dict = {}
    for intent, _s, match, sim in misses:
        if match and sim is not None and sim >= ROOM_MATCH_AT:
            near.setdefault(match, []).append((sim, intent))
    out = {
        "searches": len(rows),
        "no_fit": len(misses),
        "skills_in_room": conn.execute("SELECT COUNT(*) FROM room_skills").fetchone()[0],
        "room_skills_matched (comeback candidates)": [
            {"skill": n, "searches": len(v), "best_similarity": max(v)[0],
             "examples": [i[:120] for _, i in sorted(v, reverse=True)[:3]]}
            for n, v in sorted(near.items(), key=lambda kv: -len(kv[1]))],
        "no_fit_examples": [r[0][:120] for r in misses[-5:]],
    }
    if len(rows) < FEW_SEARCHES:
        out["note"] = (f"Only {len(rows)} searches in {NO_FIT_DAYS} days: "
                       "too few to judge the room, give it one more week.")
    return out

def _rel_to_collection(path: str) -> str:
    folder = Path(path).resolve().parent
    root = SKILLS_DIR.resolve()
    return str(folder.relative_to(root)) if folder.is_relative_to(root) else str(folder)

# ---------------------------------------------------------------- entry

if __name__ == "__main__":
    mcp.run(transport="stdio")
