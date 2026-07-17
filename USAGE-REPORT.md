# Five weeks in production — usage report

Snapshot taken **2026-07-17**, covering 2026-06-10 (deploy day) → 2026-07-17. All numbers straight from `~/.skill_librarian/librarian.db` — the append-only logs the librarian keeps for itself.

## The headline numbers

| Metric | Value |
|---|---|
| Skills indexed | 3,030 |
| Queries served | 87 (86 `find`, 1 `brainstorm`) |
| Active days | 18 of 38 |
| Outcome reports filed by agents | 44 (on 41 distinct skills) |
| Report rate | ~51% of find queries got an outcome report |
| Reported **worked** | 21 |
| Reported **didn't work / not used** | 23 |
| Skills surfaced at least once | 226 (7.5%) |
| Skills never surfaced | 2,804 (92.5%) |

## Does the feedback loop actually work?

**Yes — and the notes are the best part.** Agents don't just file `worked: false`, they say why:

> *"Menubar/Tuist focus; Relay is a multiplatform NavigationSplitView app — rejected pick, using BUILD_PROMPT + project specs instead"*

> *"Codex-specific ($CODEX_HOME) — not applicable for installing a GitHub skill into Claude Code — did it manually instead."*

> *"Good launch checklist — safety scan, README ordering, topics/description, llms.txt all applied."*

Reading the 23 negative reports closely: most are **"not relevant / not used"** — the agent looked at the recommendation and solved the task another way. Only a minority are "used it and it fell short." So the negative signal is mostly measuring **ranking precision**, not skill quality. That distinction matters for curation (see improvements below).

## What gets recommended

Hot skills (most recommended over 5 weeks): `mcp-building` (6), `apple-hig-swiftui-macos` (5), `macos-menubar-swiftui` (5), `mcp-server-patterns` (5), `macos-design-guidelines` (4). Exactly the shape of the actual work — MCP servers and native macOS apps.

The HIG family is the success story: `apple-hig-swiftui-macos`, `apple-hig-settings`, `apple-hig-inspectors`, `apple-hig-macos-window-layout` — **8 reports, all worked**. When the intent matches a well-scoped skill, the pipeline nails it.

## Anti-monotony: working, with one sharp edge

The recency decay demonstrably prevents the v1 failure mode (same 3 skills every time) — 226 distinct skills surfaced across 87 queries. But it cuts both ways: `public-repo-launch` was recommended, used, and reported **worked** for one repo launch; the next day, a nearly identical launch intent got a *different* pick because the winner was recency-penalized. Punishing proven, just-confirmed winners is the one place the decay overcorrects.

## Gap analysis (retroactive)

All 87 logged queries re-embedded against the current index to find intents the collection can't serve:

- best-match similarity: **min 0.608, median 0.736, max 0.897**
- queries below the planned 0.45 gap threshold: **0** — below 0.55: **0**

Finding: with `nomic-embed-text`, cosine similarities live in a compressed 0.6–0.9 band. The planned `librarian_gaps` threshold of 0.45 would literally never fire. Real gaps *do* exist (the lowest-sim queries with a failed outcome are genuine misses — e.g. "SF Symbol HTML comments + YAML frontmatter repair" best-matched `ui-typography` at 0.614 and was reported not relevant) — but they hide inside the band. A gap detector here needs a distribution-relative threshold (bottom decile ≈ <0.65) **combined with** the outcome signal, not an absolute cutoff.

## Verdict on the scoring system

- **Outcome logging: works.** Half of all queries get a report, unprompted quality in the notes.
- **Success rate in ranking: present but dormant.** It's displayed to agents (and deliberately *not* used to re-rank — curation stays with the human), but only 3 skills have ≥2 uses, so the signal is too thin to act on yet.
- **`librarian_stats` kill-list: not actionable yet.** 92.5% never-surfaced after 87 queries says more about 87 being a small number than about the skills. The `low_success_rate` list (needs ≥3 uses) is still empty. The cull stays usage-driven and patient.

## Improvements queued from this data

1. **Log `best_sim` in `query_log` at query time** — one line — so `librarian_gaps` becomes a cheap SELECT instead of a re-embedding pass.
2. **`librarian_gaps` with a distribution-relative threshold** (bottom decile + failed outcome), not the absolute 0.45 from the original plan.
3. **Split the outcome vocabulary**: "not relevant / didn't use" vs "used and failed" are different signals (ranking precision vs skill quality). Cheapest version: an optional `used` flag on `librarian_report`.
4. **Success clears recency**: a `worked: true` report could reset the recency penalty for that skill, so proven winners aren't hidden from the very next similar intent.
