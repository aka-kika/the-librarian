# Changelog — skill_librarian_mcp

All notable changes and the decisions behind them. Newest first.

## 2026-08-04 (mcp SDK 2.0 migration)

### Changed — migrated off the removed FastMCP API
The `mcp` Python SDK 2.0 deleted `mcp.server.fastmcp.FastMCP`; the server now
uses `mcp.server.mcpserver.MCPServer` and the venv is upgraded 1.27.2 → 2.0.0
(`uv pip install --python .venv/bin/python "mcp>=2.0.0"`). The call counter no
longer monkeypatches the private `_mcp_server.request_handlers[CallToolRequest]`
(gone in 2.0) — it is now first-class server middleware
(`MCPServer(..., middleware=[_count_tool_calls])`) that logs `tools/call` from
`ctx.method`/`ctx.params`, with client info read from `ctx.session.client_params`
(handles both `client_info` and `clientInfo` spellings). Tool decorators,
pydantic param models, annotations, and `mcp.run(transport="stdio")` were
source-compatible — no changes needed. Verified 2026-08-04 with a live stdio
probe: all 5 tools listed, `FindInput` schema shape unchanged, annotations
intact, `librarian_stats` answered from the live DB (3,125 skills), counter
wrote one JSONL line with client name+version. Pre-migration code:
`server.py.bak-20260804-mcp2`. Other agents (Cursor, Grok, Goose, MiniMax)
launch the same venv — running instances keep the old code until restarted.

## 2026-07-05 (collection reorg, stats payload fix, health check)

### Changed — collection reorganized to 4 clean roots + inbox
Top level was 42 entries (source dumps, codex variants, a 4.6GB `archive.zip`); now 5: `00-INBOX/` (new-download dropzone, indexed but never wins name races), `categories/`, `kika-skills/` (all kika-authored: akakika+reshelf suites, kika-note, design-system, project gates, cursor-skills-kika, grok, from-*-installed), `v8v/`, `agents-live@`. Codex variants, `KIKA-skills-BUNDLES` (562M), `hermes-archived`, `antigravity-skill-engine`, `obsidian-skills-main`, dead `self-improving` → `_INFRA/Skills-archive/` (outside the indexed tree — nothing deleted); their 39+24 unique skills were **copied first** into `categories/bundles-rescued/` and `categories/hermes-rescued/`. Source dumps folded into categories (`ai-research-skills`, `academic-research-skills`, `prompt-master`, `marketing-skills`). `archive.zip` → `_INFRA/backups/Skills-collection-archive-20260610.zip`. Pre-reorg DB + manifests: `_INFRA/backups/skills-reorg-20260705/`.

`LIBRARIAN_CANONICAL_PREFIXES` → `categories,v8v,kika-skills` in all 5 agent configs (Claude/Cursor/Grok/Goose/MiniMax, backups in `backups/configs/`). Reindex: 3,019 → 3,019 (added `reshelf-groundskeeper` — newer than the last index; pruned `academic-research-suite` — codex-only, archived), 82 re-embeds, winners now 2,722 categories / 115 kika-skills / 93 v8v / 89 agents-live. Rescued skills verified ranking from new paths (`minecraft-modpack-server` 0.747, `threejs-shaders` 0.776, both fit-10 picks). Restart clients to load the new prefix env.

**Kika's old-Swift hunch checked:** not confirmed — only 7 skills even mention pre-iOS-15/old-Xcode signals, all as availability notes or migration-away guides (vision-framework, core-ml, webkit-integration…). `apple-macos` is modern (StoreKit 2, SwiftData, Liquid Glass, Swift Testing). Nothing worth purging.

**Phase 2 candidates (inside `categories/`, deliberately untouched today):** `impeccable` vs `impeccable-main` (+ a `node_modules` inside!), `agent-skills` vs `agent-skills-main`, junk-named `sorttt`, `summarize`, `Database-cursor\` (trailing backslash), `SkillClaw`, `general`; `agent-scripts-user/cursor/` copies still shadow `agent-scripts/skills/` by ASCII order.

### Changed — kika's same-day corrections to the reorg
`from-*-installed` were mostly NOT hers: kept `session`/`ship`/`github`/`repo-docs` in `kika-skills/`, moved the rest to `categories/from-{claude,cursor}-installed/`. Her `old-project-audit` v1.1.0 stays canonical; the older claude-installed copy → `Skills-archive/dupes/`. `bundles-rescued` + `hermes-rescued` moved from categories into `kika-skills/` ("I wanna see it always"). `agents-live@` symlink tucked inside `categories/`. Top level is now exactly: `00-INBOX / categories / kika-skills / v8v / README.md`. Reindex: 3,016 (pruned 3 `andruia-*` — deliberately removed in the 2026-07-04 cursor cleanup, backed up in `backups/cursor-skills-cleanup-20260704/removed-bulk-pack/`; their collection copies vanished via file sync). Winners: 2,757 categories / 166 kika-skills / 93 v8v.

### Fixed — `librarian_stats` blew past tool-response limits
With a young `query_log` (39 queries), 2,897 of 3,019 skills had never surfaced, and stats dumped every name — an 84KB payload that Claude Code refuses to inline (dumps to a file instead). `never_surfaced` is now `{count, sample}` — 20 names evenly spread across the sorted list — plus a pointer to the DB for the full list. Payload: 84,617 → ~1,600 chars. Also dropped the dead `low_match_queries` local (leftover scaffolding for the planned `librarian_gaps` tool). Restart clients to pick up.

### Finding — full-pipeline health check, all green
Index: 3,019 skills = 3,019 embeddings, dedupe correct. Ranking: functional intent check passes, MMR reorder visible, outcome feedback loop live (`mcp-server-patterns` surfaces with its 0.0 success rate). AFM rerank: binary healthy (~4–6s for 3–5 candidates); the session's *first* find fell back to embed order (cold start) and reranked fine from the second call on — silent-fallback-by-design, but note there is zero logging when it happens. Usage: 39 real queries, 12 outcomes (6 worked / 6 not); failure notes are already surfacing collection gaps (no claude-code-config/MCP-management skill, no installed-skills-triage skill).

## 2026-06-17 (multi-client rollout, trim, codex rescue, parser fix)

### Fixed — `parse_skill` frontmatter description extraction
Two valid-YAML cases were mis-parsed, giving 42 indexed skills junk descriptions that ranked badly:
1. `description:` with the value on the *following indented lines* (plain multiline, no `|`/`>` block scalar) — the parser read empty, then the fallback grabbed the first frontmatter line (`name:`/`category:`). Fixed by treating an empty `description:` value like a block scalar (gather indented continuation).
2. No `description:` field at all — the fallback scanned the whole file and grabbed a frontmatter `key: value` line. Fixed: fallback now searches the body *after* the frontmatter, de-blockquotes (`>`), and skips headings/fences.
Result: 40/42 auto-fixed on reindex; 2 source files patched by hand (`agentos` had no description; `insta-tiktok-reels` was missing its opening `---`). Junk/short descriptions now 0.
**Operational note:** running MCP servers cache the old parser in memory — restart each client (all 6) before reindexing from it, or an old-code reindex will re-break these descriptions. A standalone `reindex_collection()` must be run with `LIBRARIAN_SKILLS_DIR` set, or it scans the default `~/.claude/skills` and prunes the real collection (recovered same day by re-running with env set).

### Changed — collection curation (2,855 → 2,897)
Trimmed 27 (enterprise CRM/devops, games/media, raw social scrapers) to reversible quarantine; rescued 70 new curated Codex skills + upgraded 14 stale collection copies to their polished Codex versions before Codex Desktop decommission; quarantined 1 inert placeholder (`template-skill`).

## 2026-06-10 (one big day: build → deploy → Apple integration)

### Changed — `LIBRARIAN_CANONICAL_PREFIXES` now `categories,v8v`
kika's own freshly-optimized skills live under `v8v/` (minimax re-tuned the apple-hig set for the macOS 27 changes on 2026-06-09); bundle shadows in `KIKA-skills-BUNDLES/` were winning name races on ASCII order. Index winners for all apple-hig skills now point at `v8v/01-apple-hig-mastery/`. Content was verified byte-identical between the two trees (minimax's run touched both), so embeddings were already serving the optimized text — this fixes provenance for `librarian_stats` curation.

### Added — live working set joined the index (+97 skills, now 2,552)
`Skills-collection/agents-live` → symlink to `~/.agents/skills`. 97 of kika's daily-driver skills (higgsfield-*, architecting, daily-log, mcp-building…) existed only in the live working set, invisible to the librarian. The walker follows symlinks, so they stay in sync with no copies; `categories/` still wins duplicate-name races.
- Verified live over MCP: "generate a product photoshoot image for my brand" → AFM rerank picks `higgsfield-product-photoshoot` (fit 10) over a higher-similarity generic skill. Embeddings find, the reranker chooses.
- Also confirmed in live testing: recency decay benched `app-icon-generator` after sanity-check queries returned it — anti-monotony working as designed; it ages out of the 10-query window naturally. Test queries pollute the log briefly; acceptable, logs stay append-only.

### Added — AFM reranker (`afm_rerank.swift`, `bin/afm-rerank`)
`librarian_find` can now pass its shortlist through Apple's on-device Foundation Model, which scores each candidate's fit (1–10), gives one sentence why / why-not, and names a final pick. ~6–12s, fully local, free.
- **Decision:** build our own ~100-line Swift CLI instead of depending on afm-cli/apfel (researched both) — zero deps, guided generation (`@Generable`) guarantees valid JSON, tailor-made I/O.
- **Decision:** rerank runs AFTER MMR + recency decay, so anti-monotony survives. Brainstorm is never reranked — it's for divergence.
- **Decision:** never trust the model's output — it duplicated candidate names in testing. Server validates every ranked name against the real candidate set; failures/timeouts fall back silently to embedding order.
- Opt-in via `LIBRARIAN_RERANK_BIN` env; unset = old behavior.

### Added — Apple's Xcode 27 agent skills in the collection
7 skills (swiftui-specialist, swiftui-whats-new-27, test-modernizer, uikit-app-modernization, c-bounds-safety, device-interaction, audit-xcode-security-settings) exported from Xcode 27 beta and added at `categories/apple-macos/xcode-27-agent-skills/`. Index: 2,448 → 2,455.
- Gotcha: `xcode-select` points at CommandLineTools — export needs `DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer xcrun agent skills export`.
- Re-export at Xcode 27 GA (article mentions 10 skills; beta ships 7).

### Added — `LIBRARIAN_CANONICAL_PREFIXES` env
When the same skill name exists at several paths, copies under listed top-level dirs (set: `categories`) win the dedupe race.
- **Why:** winners were picked by ASCII path order, so bundle exports (`KIKA-skills-BUNDLES`, `v8v`) beat kika's curated `categories/` tree. Now 2,140 of 2,455 winners come from `categories/`.

### Fixed — YAML block-scalar descriptions (113 skills)
`description: >-` / `|` frontmatter was parsed as the literal characters `>-`. Parser now joins the indented block. 113 skills got real descriptions; full re-embed followed.

### Fixed — change-detection hash too narrow
Hash covered only the body, so name/description changes (incl. parser fixes) never triggered re-embeds. Now hashes the full searchable surface (name + description + body).

### Fixed — Ollama 500 on dense-script skills
Legacy `/api/embeddings` hard-fails when input exceeds the model's token context — Korean text hits the limit at ~3× fewer chars. Switched to modern `/api/embed` with `truncate: true`. (10 Korean-language skills were crashing every reindex.)

### Fixed — reindex lost all progress on one bad embed
Single commit at the end meant any mid-run exception rolled back everything. Now commits every 50 changes.

### Fixed — quoted YAML names
`name: "foo"` kept its quotes, breaking `librarian_report` name matching. Quotes stripped.

### Changed — collection pointed at the real thing
`LIBRARIAN_SKILLS_DIR` → `~/_KIKA_MAIN/Skills-collection` (kika confirmed canonical; 4,048 files → 2,448 unique). Earlier guesses (`~/.claude/skills`, `~/.agents/skills`) were symlink farms/subsets.

### Finding — no static junk in the collection
Scanned all unique skills for stubs/placeholders/missing descriptions: every suspect was a false positive (e.g. typography skills *mentioning* "lorem ipsum"). Culling must be usage-driven via `librarian_stats` — that's tasks 3–4.

### Fixed — symlinked skills silently skipped
`Path.rglob` doesn't follow directory symlinks on Python 3.12. Replaced with `os.walk(followlinks=True)` + realpath dedup.

### Fixed — `ON DELETE CASCADE` never fired
SQLite has foreign keys off by default; pruned skills left orphan embedding rows. `PRAGMA foreign_keys=ON` added in `db()`.

### Deployed
uv venv (Python 3.12) at `.venv`, registered as `skill-librarian` in `~/.claude.json` (pre-edit backup: `~/.claude.json.bak-librarian`). Full reindex ~2 min local; rankings verified on real intents (e.g. "build an mcp server" → `mcp-builder` 0.74).

### Built (Claude.ai session)
`server.py` v1: 5 tools (find / brainstorm / report / reindex / stats), Ollama embeddings, MMR + recency decay + wildcards, SQLite WAL, append-only logs.
