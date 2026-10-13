"""Run the 2026-10-09 decision-filter test set through nimble-v3 and old nimble.

Usage: python3 -I run_v3.py <bench_dir> <out_dir>
Writes res_<model>.json in out_dir, same shape as the bench's own files.
"""
import json, sys, time, urllib.request

bench, out = sys.argv[1], sys.argv[2]
ts = json.load(open(f"{bench}/testset.json"))
INS = "Does this skill help accomplish the user's intent? Judge only from the skill description."


def post(path, d):
    r = urllib.request.Request("http://127.0.0.1:11434" + path, json.dumps(d).encode(),
                               {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=300))


def v3(m, q, n, d):
    st = f"Intent: {q}\nSkill: {n} - {d[:600]}"
    a = post("/v1/systemone", {"model": m, "state": st, "questions": {
        "fit": {"type": "score", "instructions": INS,
                "criteria": ["unrelated", "weak", "good", "perfect"]},
        "rel": {"type": "noul", "instructions": INS, "criteria": {
            "true": "The skill is directly useful for this task.",
            "false": "The skill is unrelated, too generic, or for a different task."}}}})["answers"]
    return {"score": a["fit"]["score"], "noul": a["rel"]["noul"]}


def old(m, q, n, d):
    # Same prompt as the bench's nimble() in run.py.
    p = f"""Schema:
field: fit
meaning: Does the skill help accomplish the user's intent? Judge only from the skill description.
choices:
A: yes, the skill is directly useful for this task
B: no, the skill is unrelated, too generic, or for a different task

Context:
Intent: {q}
Skill: {n} - {d[:600]}

Field: fit
Answer:"""
    o = post("/api/generate", {"model": m, "prompt": p, "stream": False, "think": False,
                               "options": {"temperature": 0, "num_predict": 3}})["response"]
    return {"keep": o.strip().upper().startswith("A"), "raw": o}


for m, f in (("nimble-v3:latest", v3), ("nimble:latest", old)):
    f(m, ts[0]["q"], "x", "y")  # warm up
    res, t0 = [], time.time()
    for ti, t in enumerate(ts):
        for c in t["cands"]:
            t1 = time.time()
            r = f(m, t["q"], c["skill"], c["desc"])
            r.update(t=time.time() - t1, ti=ti, skill=c["skill"])
            res.append(r)
    tot = time.time() - t0
    ps = [x for x in json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/ps"))["models"]
          if x["name"] == m]
    json.dump({"res": res, "total": tot, "mem": ps[0]["size"] if ps else None},
              open(f"{out}/res_{m.replace(':', '_')}.json", "w"))
    print(m, "done", round(tot), "s", flush=True)
