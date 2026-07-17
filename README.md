# the_librarian

A local-first MCP server that acts as a **librarian** between coding agents and a large skill collection (~3,000 skills). The collection stays raw markdown on disk — agents never load it into context. They ask; the librarian retrieves (embedding search + MMR diversity + recency decay), optionally deliberates with Apple's on-device Foundation Model, and learns from reported outcomes.

Core principle: **agents read recommendations, write only outcomes.** They never edit skills, weights, or rankings. Curation decisions stay with the human, informed by `librarian_stats`.

Every stage is local and free: Ollama embeddings, SQLite, and (optionally) Apple Intelligence.

**→ [Five weeks in production: real usage numbers](USAGE-REPORT.md)** — 87 queries, 44 agent-filed outcome reports, what worked and what's queued next.

## Pipeline (one find request, end to end)

```
intent ─→ Ollama embed (nomic-embed-text, /api/embed)
       ─→ cosine vs ~3,000 skill vectors (SQLite)
       ─→ MMR diversity + recency decay              ← anti-monotony
       ─→ [opt-in] AFM rerank: Apple's on-device model scores fit 1–10,
          why / why-not per candidate, final pick     (bin/afm-rerank, ~6–12s)
       ─→ compact JSON shortlist
```

## Setup (2 minutes)

```bash
cd the_librarian
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "mcp[cli]" httpx pydantic

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

The skills dir is scanned recursively for `SKILL.md` files (standard agent-skill format: YAML frontmatter with `name` + `description`, body below). `OLLAMA_HOST` can point at a remote node (e.g. over Tailscale) to offload embedding — though an M4 Max reindexes 3,000 skills in ~2 minutes locally.

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
| `librarian_find` | Ranked shortlist for an intent. Diversity-adjusted, recency-decayed, shows success rates. |
| `librarian_brainstorm` | Wide diverse sweep + 2 random wildcards. For ideation, not convergence. |
| `librarian_report` | Agent reports skill worked/failed. Append-only — agents can't touch rankings. |
| `librarian_reindex` | Rescan + re-embed changed SKILL.md files (hash-checked, cheap to rerun). |
| `librarian_stats` | Curation digest: hot skills, never-surfaced (kill candidates), low success rate (rewrite candidates). |

## Why it won't recommend the same 3 skills forever

The failure mode this design exists to kill: a naive retriever recommends the same handful of skills every time. Three mechanisms prevent it:

1. **MMR selection** — penalizes candidates too similar to ones already picked in the same response.
2. **Recency decay** — anything recommended in the last 10 queries gets downweighted (`RECENCY_PENALTY = 0.15` per appearance — tune at top of server.py).
3. **Wildcards in brainstorm mode** — 2 random skills from outside the relevant set, every time.

The AFM reranker runs *after* these — it reorders the already-diversified shortlist, so it can't reintroduce monotony.

Five weeks of real data says it works: 226 distinct skills surfaced across 87 queries. See the [usage report](USAGE-REPORT.md) for the one sharp edge (recency decay also hides just-confirmed winners) and the fix queued for it.

## The flywheel

Every `librarian_find` is logged. Every `librarian_report` is logged. Run `librarian_stats` weekly: never-surfaced skills → kill, low-success skills → rewrite descriptions, queries matching nothing → skills you should build. The collection curates itself from usage.

## Tuning knobs (top of server.py)

- `RECENCY_WINDOW` / `RECENCY_PENALTY` — how hard repeats get punished
- `MMR_LAMBDA_FIND` (0.7) — relevance vs diversity for find
- `MMR_LAMBDA_BRAINSTORM` (0.45) — brainstorm leans diverse
- `RERANK_TIMEOUT` (30s) — AFM reranker budget before falling back to embed order

Env knobs: `LIBRARIAN_CANONICAL_PREFIXES` (which top-level dirs win duplicate-name races, e.g. `categories`), `LIBRARIAN_RERANK_BIN` (path to afm-rerank; empty disables rerank).

## Hard rules the code keeps

- Never load full skill bodies into tool responses — descriptions + metadata only. The whole point is keeping the collection out of agent context.
- Agents get no write access beyond the append-only outcome log.
- DB is SQLite WAL at `~/.skill_librarian/librarian.db`; `query_log` and `outcome_log` are append-only — no tool deletes or rewrites log rows.

See `CHANGELOG.md` for the full history of changes and the decisions behind them.
