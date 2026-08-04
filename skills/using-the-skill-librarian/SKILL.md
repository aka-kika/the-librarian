---
name: using-the-skill-librarian
description: Use when starting any non-trivial task, when wondering "is there a skill for this", or after finishing work that used a librarian recommendation. Applies in every agent connected to the skill-librarian MCP server.
---

# Using the Skill Librarian

## Overview

The `skill-librarian` MCP server searches your full skill collection and recommends
the best fits for a stated intent. **The librarian is the access path**: only a small
curated set of skills stays installed per agent; everything else is one
`librarian_find` call away. Never copy skill files into agent config directories —
read recommendations in place.

This is the one skill worth installing everywhere. It replaces the rest.

## Quick Reference

| Tool | When to call |
| --- | --- |
| `librarian_find(intent, k)` | Before any non-trivial task. Plain-language intent ("package a python mcp server for distribution"), not keywords. |
| `librarian_brainstorm` | Open-ended ideation — "what could I build/do here" — instead of find. |
| `librarian_report(skill, worked, note)` | **Always** after acting on a recommendation — used or rejected, one line why. Success rates drive curation; this is not optional bookkeeping. |
| `librarian_reindex` | After adding or editing skills in the collection. |
| `librarian_stats` | Collection health and usage stats. |

## Workflow

1. `librarian_find` with your intent. Do this even if locally-installed skills look
   sufficient — the librarian searches the whole collection; your installed list is a
   tiny fraction of it.
2. Weigh each recommendation's `fit`, `why`, and `why_not`. Rejecting all of them is a
   valid outcome.
3. Load the chosen skill from the collection — recommendations return names, not
   paths, and skills may be nested inside bundles:
   `find <your-skills-dir> -maxdepth 4 -type d -name "<skill-name>"` → read its `SKILL.md`.
4. Follow the skill.
5. `librarian_report` with worked=true/false and a one-line note (why it worked, why
   it failed, or why you rejected it).

## If the MCP isn't connected

The server is a local stdio Python process. Command and env (translate to your
agent's config format):

```json
{
  "command": "/path/to/the_librarian/.venv/bin/python",
  "args": ["/path/to/the_librarian/server.py"],
  "env": {
    "LIBRARIAN_SKILLS_DIR": "/path/to/your/skills-collection",
    "OLLAMA_HOST": "http://localhost:11434",
    "LIBRARIAN_EMBED_MODEL": "nomic-embed-text",
    "LIBRARIAN_RERANK_BIN": "/path/to/the_librarian/bin/afm-rerank"
  }
}
```

`LIBRARIAN_RERANK_BIN` is optional (macOS + Apple Intelligence only); omit it to use
pure embedding order. See the repo README for full setup.

## Common Mistakes

- **Skipping find because an installed skill looks close enough** — the collection
  version may be better; check first.
- **Keyword-style intents** ("mcp python") — write what you're trying to accomplish;
  the embedding search works on intent.
- **Forgetting `librarian_report`** — unreported uses starve the curation loop.
- **Copying a recommended skill into an agent's skills dir** — read it in place; if it
  earns permanent installation, the human decides.
