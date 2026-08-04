# The origin conversation

*Mid-day, poolside, phone in hand. Claude Fable had just come out and word was it would only be around for a few days — so this had to be a question worth asking. It was the first one that came to mind. Not because it had been on the list — it hadn't — but because the whole feed at the time was skills, skills, skills: the skill you need, the skill you didn't know you need. And when there is too much of something, run.*

*What follows is the conversation exactly as it happened — typed on a phone, typos kept on purpose. The librarian in this repo is what came out the other side.*

---

**Kika**

I'm m sure you know better then then the internet what you need to get better. What's the best workflow with Claude code? Installing skills and just you pick the best? More the better? Less is more?

---

**Claude**

Less is more, and here's the mechanism behind why: skill descriptions get loaded into context so Claude Code can decide what to trigger. Every installed skill costs tokens and adds triggering ambiguity. With 50 skills, descriptions overlap, the wrong one fires, or none fires because the matching got muddy. With 8 sharp ones, triggering is nearly deterministic.

The actual quality lever isn't quantity — it's description precision. A skill that triggers reliably on exactly the right requests beats five that sort of cover the same ground. You've already seen this with your own ecosystem: `wwdc26-audit` works because it's scoped to one verifiable thing, not "Apple dev helper."

My honest workflow recommendation:

- **Keep skills you invoke weekly.** If you haven't triggered it in a month, archive it (cold storage, ⚪ style — you already have the system for this).
- **One skill = one job.** Your "small tools, one thing, perfectly" philosophy is literally the correct skill design principle. Merge overlapping skills, split bloated ones.
- **Put project context in CLAUDE.md, not skills.** Skills are for *procedures* (how to package an MCP server, how to export a Stitch theme). Standing facts about your stack belong in CLAUDE.md files per repo — always loaded, no triggering needed.
- **Progressive disclosure inside skills.** Thin SKILL.md, heavy reference files it points to. The skill only pays its full token cost when actually used.

So no — don't install everything and let Claude pick. Curate ruthlessly, write descriptions like trigger conditions ("use when X, do NOT use for Y"), and treat the skills folder like your KIKA\_HUB active tier: small, hot, and earned.

---

**Kika**

Plugins vs skills. Go

---

**Claude**

**Skills** = a single procedure. One folder, one SKILL.md, maybe reference files. You write it, you own it, it lives in `~/.claude/skills/` or a repo. Atomic.

**Plugins** = a distribution wrapper. One plugin can bundle skills + slash commands + subagents + MCP servers + hooks, installed from a marketplace with one command, versioned, updatable.

The mental model: skills are the *content*, plugins are the *packaging*. A plugin with one skill in it is functionally just that skill — with install/update/uninstall plumbing around it.

When each wins:

- **Skills win for your own stuff.** Your `wwdc26-audit`, design-extractor, prompt-master — these are yours, iterated constantly. Wrapping them in plugin packaging adds friction with zero benefit. Edit the file, done.
- **Plugins win for distribution and composition.** Sharing across machines or with others, or when the thing genuinely needs multiple parts — a skill *plus* an MCP server *plus* a hook that fire together. Your MetaClaw routing setup is conceptually plugin-shaped: skill + server + trigger logic as one unit.
- **Plugins also win for team/marketplace consumption** — install someone's curated bundle, get updates, uninstall cleanly without orphaned files.

The trap, given yesterday's "less is more" answer: plugin marketplaces make over-installing *easy*. One plugin can dump 15 skills into your context. Audit what a plugin actually contains before installing — you're paying the token cost for every skill inside it, not just the ones you wanted.

For you specifically: keep authoring as bare skills, your Dropbox + symlink setup already solves your cross-machine distribution. Consider a plugin only if you ever ship the ollama\_mcp ecosystem publicly — that's a skill + MCP server bundle, which is exactly what plugins exist for.

---

**Kika**

My dream is to make a "talk to skills" app. I did one. Was 90% ok. It was just recommending same skills always. To have a collection that is getting updated, adjusted, optimized, and then keep it out of reach of agents, but use ai to talk to the collection raw text and brainstorm what skill I can use now

---

**Claude**

The "always recommends the same skills" failure is diagnosable, and it's almost never the model — it's the retrieval setup. Two usual culprits: you're dumping all descriptions into one prompt (the model anchors on the most generic-sounding ones, every time), or your descriptions overlap so much that nothing distinguishes them semantically. Generic descriptions = generic recommendations, forever.

The architecture you're describing is a **skill librarian** — and it's genuinely the right pattern: collection stays raw text on disk, agents never load it, one dedicated layer mediates. Here's how I'd build v2:

- **Embed, don't prompt-stuff.** Index every SKILL.md locally (your Ollama cluster — `nomic-embed-text` on the Alienware is free and fast). Query → top-8 candidates by similarity, *then* an LLM reranks just those 8 with reasoning. Small candidate set = the model actually deliberates instead of pattern-matching.
- **Kill the monotony with MMR + usage decay.** Maximal marginal relevance penalizes candidates too similar to ones already picked, and store a `last_recommended` timestamp per skill — downweight anything suggested in the past N queries. This single change probably fixes your 90% → same-3-skills problem.
- **Make it answer "why not."** Force the output format: 3 candidates, one sentence each on why it fits, one on why it might not, then a pick. Brainstorming mode falls out of this naturally — it's the same query with "don't converge, give me angles" instead.
- **Ship it as an MCP server.** `skill-librarian` with two tools: `find_skill(intent)` and `brainstorm(context)`. Agents can *ask* the librarian without ever ingesting the collection — exactly the out-of-reach property you want. SQLite + embeddings, fully local-first, one job.

The poetic part: the librarian is itself the curation loop. Log every query and what got picked vs ignored — skills that never surface are your kill candidates, queries that match nothing are your gaps. The collection optimizes itself from its own usage data.

Want me to sketch the FastMCP scaffold for it? It's structurally a sibling of your ollama\_mcp server.

---

**Claude**

Yes — let's build it now so it's waiting for you. Let me check the MCP scaffolding reference first.

---

**Kika**

can you make a claude md file, i wil span you to the claude code with this task

---

**Claude**

Here it is. Drop all three files (`server.py`, `README.md`, `CLAUDE.md`) into `~/KIKA_HUB/INFRA/skill_librarian/`, open Claude Code there, and just say "do task 1 from CLAUDE.md."

It's written so Claude Code can't drift: status says the server is done (no rewrites), the three anti-monotony mechanisms are marked as untouchable, agents-write-only-outcomes is a hard rule, and tasks are ordered — deploy first, Tailscale check second, the `librarian_gaps` tool only after those work.

---

*And that was it. The files landed, Claude Code deployed them, and the librarian has been answering `find_skill(intent)` ever since — [the numbers it kept about itself are here](USAGE-REPORT.md).*
