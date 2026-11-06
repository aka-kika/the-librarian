"""Run the 2026-10-09 decision-filter test set through decision models on /v1/systemone.

Usage: python3 -I run.py <bench_dir> <out_dir> <model> [<model> ...]
Same prompt and questions as nimble-v3-2026-10-13/run_v3.py, so results compare 1:1.
Writes res_<model>.json in out_dir.
"""
import json, sys, time, urllib.request

bench, out, models = sys.argv[1], sys.argv[2], sys.argv[3:]
ts = json.load(open(f"{bench}/testset.json"))
INS = "Does this skill help accomplish the user's intent? Judge only from the skill description."


def post(path, d):
    r = urllib.request.Request("http://127.0.0.1:11434" + path, json.dumps(d).encode(),
                               {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=300))


def ask(m, q, n, d):
    st = f"Intent: {q}\nSkill: {n} - {d[:600]}"
    a = post("/v1/systemone", {"model": m, "state": st, "questions": {
        "fit": {"type": "score", "instructions": INS,
                "criteria": ["unrelated", "weak", "good", "perfect"]},
        "rel": {"type": "noul", "instructions": INS, "criteria": {
            "true": "The skill is directly useful for this task.",
            "false": "The skill is unrelated, too generic, or for a different task."}}}})["answers"]
    return {"score": a["fit"]["score"], "noul": a["rel"]["noul"]}


for m in models:
    ask(m, ts[0]["q"], "x", "y")  # warm up
    res, t0 = [], time.time()
    for ti, t in enumerate(ts):
        for c in t["cands"]:
            t1 = time.time()
            r = ask(m, t["q"], c["skill"], c["desc"])
            r.update(t=time.time() - t1, ti=ti, skill=c["skill"])
            res.append(r)
    tot = time.time() - t0
    ps = [x for x in json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/ps"))["models"]
          if x["name"] == m]
    json.dump({"res": res, "total": tot, "mem": ps[0]["size"] if ps else None},
              open(f"{out}/res_{m.replace(':', '_')}.json", "w"))
    print(m, "done", round(tot), "s", flush=True)
    if m != "nimble-v3:latest":  # the live librarian filter uses nimble-v3, leave it loaded
        post("/api/generate", {"model": m, "keep_alive": 0})
