"""FMD 2026-10-16 review of the nimble-v3 soft-filter trial (started 2026-10-13).

Reads ~/.skill_librarian/soft_filter.jsonl and outcome_log, and prints:
- how many finds were judged, skipped, or needed the old-nimble fallback
- time added per find
- how often v3 changed the order of the top candidates
- for each librarian_report after a judged find (within 2 h): was the
  reported skill marked likely or weak, and did it work

Usage: ~/_KIKA_MAIN/_INFRA/mcp/servers/the_librarian/.venv/bin/python fmd_review.py
"""
import json, sqlite3, statistics
from pathlib import Path

TRIAL_START = 1791916605  # 2026-10-13 21:36 local
LOG = Path("~/.skill_librarian/soft_filter.jsonl").expanduser()
DB = Path("~/.skill_librarian/librarian.db").expanduser()

finds = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
before = [f for f in finds if f["ts"] < TRIAL_START]
trial = [f for f in finds if f["ts"] >= TRIAL_START]
# Agent sessions started before the trial still run the old server code: it
# sends v3 a text prompt, gets nothing and logs a skip with no fallback.
# New code always falls back, so those lines are left out.
stale = [f for f in trial if f.get("model", "").startswith("nimble-v3")
         and f.get("status") != "ok" and not f.get("fallback")]
trial = [f for f in trial if f not in stale]
print(f"(left out {len(stale)} finds from sessions still on the old code)")


def summary(name, fs):
    ok = [f for f in fs if f.get("status") == "ok"]
    ms = [f["ms"] for f in ok]
    moved = sum(1 for f in ok if (f.get("likely", []) + f.get("weak", [])) != f["order_in"])
    print(f"== {name}: {len(fs)} finds, {len(ok)} judged, {len(fs) - len(ok)} skipped, "
          f"{sum(1 for f in fs if f.get('fallback'))} fell back to old nimble")
    if ms:
        print(f"   time added: median {statistics.median(ms)} ms, max {max(ms)} ms")
        print(f"   order changed in {moved} of {len(ok)} judged finds")
        print(f"   no likely skill at all: {sum(1 for f in ok if not f.get('likely'))}")
    return ok


summary("before the trial (old nimble)", before)
ok = summary("nimble-v3 trial", trial)

conn = sqlite3.connect(DB)
reports = conn.execute("select ts, skill_name, worked, note from outcome_log where ts >= ? order by ts",
                       (TRIAL_START,)).fetchall()
print(f"== reports since the trial started: {len(reports)}")
right = wrong = 0
for ts, skill, worked, note in reports:
    f = next((f for f in reversed(ok) if f["ts"] <= ts <= f["ts"] + 7200 and skill in f["order_in"]), None)
    if not f:
        print(f"   {skill}: no judged find matched")
        continue
    label = "likely" if skill in f.get("likely", []) else "weak" if skill in f.get("weak", []) else "not judged"
    i = f["order_in"].index(skill)
    prob = (f.get("probs") or [None] * 9)[i]
    fair = (label == "likely") == bool(worked)
    right += fair
    wrong += not fair
    print(f"   {skill}: marked {label} (p={prob}), worked={bool(worked)} -> {'right' if fair else 'WRONG'}"
          f"  | {f['intent'][:70]}")
print(f"== labels checked against reports: {right} right, {wrong} wrong")
