# the_librarian

**A skill librarian MCP server — your agents stop carrying the whole skill collection in context and start asking for the one right book.**

![the_librarian — a wall of thousands of muted books; through the one gap, a robot hand pulls out the single orange book](assets/banner.webp)

![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey) ![Python 3.12+](https://img.shields.io/badge/python-3.12+-lightgrey) ![MCP server](https://img.shields.io/badge/MCP-server-lightgrey) ![Local-first](https://img.shields.io/badge/local--first-no%20cloud-lightgrey)

## How it started

One question in a chat:

> *"I'm sure you know better than the internet what you need to get better. What's the best workflow with Claude Code? Installing skills and just you pick the best? More the better? Less is more?"*

The answer: **less is more.** Every installed skill costs tokens and adds triggering ambiguity — with 50 skills, descriptions overlap and the wrong one fires; with 8 sharp ones, triggering is nearly deterministic.

But the collection had ~3,000. So instead of installing any of them: give the collection a librarian.

- `find_skill(intent)` → librarian recommends
- agent uses the skill, does the work
- `report_outcome(skill, worked: true/false, note)` → librarian logs it

> *"The poetic part: the librarian is itself the curation loop. Log every query and what got picked vs ignored — skills that never surface are your kill candidates, queries that match nothing are your gaps. The collection optimizes itself from its own usage data."*

> *"So the full shape: SQLite (skills, embeddings, query log, outcome log) + FastMCP + two read tools + one write tool. Maybe 300 lines of Python. It's small, it's one thing, and it compounds."*

It's ~600 lines now. Scope creep found even the librarian. The original conversation — typed poolside on a phone, typos preserved on purpose — is in [ORIGIN.md](ORIGIN.md).

## What it is

A local-first MCP server that acts as a **librarian** between coding agents and a large skill collection. The collection stays raw markdown on disk — agents never load it into context. They describe what they're trying to do in plain language; the librarian finds the skills whose *meaning* matches (semantic search — no keyword guessing), makes sure the shortlist isn't three flavors of the same thing, avoids repeating what it just recommended, optionally lets Apple's on-device model deliberate over the finalists — and learns from what agents report back.

Core principle: **agents read recommendations, write only outcomes.** They never edit skills, weights, or rankings. Curation decisions stay with the human, informed by `librarian_stats`.

Every stage is local and free: Ollama embeddings, SQLite, and (optionally) Apple Intelligence.

**→ [In production since June: real usage numbers](USAGE-REPORT.md)** — 366 queries, 284 agent-filed outcome reports across three snapshots (latest 2026-09-12), what worked and what's queued next.

## Pipeline (one find request, end to end)

In plain words: every skill's description is turned once into an *embedding* — a list of numbers that captures what the skill is about, so "package my server for distribution" can match a skill that never uses the word "package." A request goes through five steps:

```
intent ─→ Ollama embed (nomic-embed-text, /api/embed)
       ─→ cosine vs every skill vector (SQLite)        ← "which skills mean the same thing?"
       ─→ MMR diversity + recency decay                ← anti-monotony (see below)
       ─→ [opt-in] AFM rerank: Apple's on-device model scores fit 1–10,
          why / why-not per candidate, final pick       (bin/afm-rerank, ~6–12s)
       ─→ compact JSON shortlist
```

## Setup (2 minutes)

```bash
cd the_librarian
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "mcp[cli]>=2" httpx pydantic

# embedding model on whichever node you point at:
ollama pull nomic-embed-text
```

## Claude Code config

Add to `~/.claude.json` (or project `.mcp.json`), with paths adjusted to where you cloned this and where your skills live:

```json
{
  "mcpServers": {
    "skill-librarian": {
      "command": "/path/to/the_librarian/.venv/bin/python",
      "args": ["/path/to/the_librarian/server.py"],
      "env": {
        "LIBRARIAN_SKILLS_DIR": "/path/to/your/skills-collection",
        "OLLAMA_HOST": "http://localhost:11434",
        "LIBRARIAN_EMBED_MODEL": "nomic-embed-text",
        "LIBRARIAN_CANONICAL_PREFIXES": "categories",
        "LIBRARIAN_RERANK_BIN": "/path/to/the_librarian/bin/afm-rerank"
      }
    }
  }
}
```

The skills dir is scanned recursively for `SKILL.md` files (standard agent-skill format: YAML frontmatter with `name` + `description`, body below). `OLLAMA_HOST` can point at a remote node (e.g. over Tailscale) to offload embedding — though an M4 Max reindexed the original 3,000-skill collection in ~2 minutes locally (it has since been trimmed to under 900, which takes seconds).

## First run

```
> use librarian_reindex
> use librarian_find with intent "package my ollama wrapper for distribution"
```

## AFM reranker (optional, Apple Intelligence)

`librarian_find` can pass its shortlist through Apple's on-device Foundation Model for a deliberation pass — reorders by reasoned fit (1–10) and adds one-sentence why / why-not per candidate, plus a final `pick`. Fully local, free, ~5–12s. Build and enable:

```bash
swiftc -O -parse-as-library afm_rerank.swift -o bin/afm-rerank
# then set LIBRARIAN_RERANK_BIN to the binary path in the MCP env
```

Unset `LIBRARIAN_RERANK_BIN` (or any failure/timeout) falls back to pure embedding order. Brainstorm is never reranked — it wants divergence, not convergence. The reranker's output is validated against the real candidate set — small on-device models happily hallucinate skill names.

## Tools

| Tool | What it does |
|---|---|
| `librarian_find` | Ranked shortlist for an intent — varied picks, no repeats from recent queries, success rates shown. |
| `librarian_brainstorm` | Wide diverse sweep + 2 random wildcards. For ideation, not convergence. |
| `librarian_report` | Agent reports skill worked/failed. Append-only — agents can't touch rankings. |
| `librarian_reindex` | Rescan + re-embed changed SKILL.md files (hash-checked, cheap to rerun). |
| `librarian_stats` | Curation digest: hot skills, never-surfaced (kill candidates), low success rate (rewrite candidates). |

## Teaching your agents to use it

Connecting the server is half the job — agents also need the habit of asking. The repo
ships one agent skill for that: [`skills/using-the-skill-librarian`](skills/using-the-skill-librarian/SKILL.md).
Install it in each agent's skills directory and it teaches the whole loop: find before
any non-trivial task, weigh the why/why-nots, read the winning skill in place, and
always file a `librarian_report` afterward — the reports are what make the collection
curate itself.

The joke writes itself, but it's real: **the only skill you install is the one that
teaches agents to ask the librarian.** In three months of production use, that habit
produced outcome reports on ~78% of queries, unprompted (~88% since this skill shipped).

## Why it won't recommend the same 3 skills forever

The failure mode this design exists to kill: a naive retriever recommends the same handful of skills every time. Three mechanisms prevent it:

1. **MMR selection** (maximal marginal relevance) — each next pick has to be relevant to the request *and* different from the picks already made, so the shortlist can't be three near-duplicates.
2. **Recency decay** — the librarian remembers its own recent recommendations; anything suggested in the last 10 queries gets pushed down (`RECENCY_PENALTY = 0.15` per appearance — tune at top of server.py).
3. **Wildcards in brainstorm mode** — 2 random skills from outside the relevant set, every time.

The AFM reranker runs *after* these — it reorders the already-diversified shortlist, so it can't reintroduce monotony.

Three months of real data says it works: 525 distinct skills surfaced across 366 queries. See the [usage report](USAGE-REPORT.md) for the one sharp edge (recency decay also hides just-confirmed winners) and the fix queued for it.

## The flywheel

Every `librarian_find` is logged. Every `librarian_report` is logged. Run `librarian_stats` weekly: never-surfaced skills → kill, low-success skills → rewrite descriptions, queries matching nothing → skills you should build. The collection curates itself from usage.

## Tuning knobs (top of server.py)

- `RECENCY_WINDOW` / `RECENCY_PENALTY` — how hard repeats get punished
- `MMR_LAMBDA_FIND` (0.7) — relevance vs diversity for find
- `MMR_LAMBDA_BRAINSTORM` (0.45) — brainstorm leans diverse
- `RERANK_TIMEOUT` (30s) — AFM reranker budget before falling back to embed order

Env knobs: `LIBRARIAN_CANONICAL_PREFIXES` (which top-level dirs win duplicate-name races, e.g. `categories`; inside one prefix the shallower path wins, so a root-level override beats the same name nested in a pack), `LIBRARIAN_RERANK_BIN` (path to afm-rerank; empty disables rerank).

## Hard rules the code keeps

- Never load full skill bodies into tool responses — descriptions + metadata only. The whole point is keeping the collection out of agent context.
- Agents get no write access beyond the append-only outcome log.
- DB is SQLite WAL at `~/.skill_librarian/librarian.db`; `query_log` and `outcome_log` are append-only — no tool deletes or rewrites log rows.

## FAQ

**What is a skill librarian?**
An MCP server that sits between AI agents and a skill collection: agents describe a task in plain language, the librarian recommends the few skills that fit, and the collection itself never enters agent context.

**Why not just install all the skills?**
Every installed skill's description is loaded into context on every request — with a large collection that's a constant token cost, and overlapping descriptions make the wrong skill fire. The librarian reduces that to one tool call when a skill is actually needed.

**Does it work with agents other than Claude Code?**
Yes — it's a standard stdio MCP server. Any MCP client (Claude Code, Claude Desktop, Cursor, Goose, and others) can use it; this one runs against five different agents daily.

**Does my skill collection leave my machine?**
No. Indexing (Ollama), storage (SQLite), and the optional reranker (Apple's on-device model) all run locally. Nothing is sent anywhere.

See `CHANGELOG.md` for the full history of changes and the decisions behind them.
