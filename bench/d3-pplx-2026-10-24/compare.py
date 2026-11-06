"""Score systemone decision models on the labelled pairs of the decision-filter test set.

Usage: python3 -I compare.py <bench_dir> <out_dir> <model> [<model> ...]
"""
import json, sys

bench, out, models = sys.argv[1], sys.argv[2], sys.argv[3:]
ts = json.load(open(f"{bench}/testset.json"))


def rows(path):
    d = json.load(open(path))
    r, i, rs = d["res"], 0, []
    for t in ts:
        for c in t["cands"]:
            rs.append({**r[i], "label": c["label"]})
            i += 1
    return d, rs


def auc(p, n):
    return sum((a > b) + 0.5 * (a == b) for a in p for b in n) / (len(p) * len(n))


def stats(rs, keep):
    L = [r for r in rs if r["label"] is not None]
    pos = [r for r in L if r["label"] == 1]
    neg = [r for r in L if r["label"] == 0]
    kp, kn = sum(map(keep, pos)), sum(map(keep, neg))
    return {"precision": round(kp / (kp + kn), 2) if kp + kn else 0,
            "good dropped": f"{len(pos) - kp}/{len(pos)}", "bad kept": f"{kn}/{len(neg)}"}


for m in models:
    d, rs = rows(f"{out}/res_{m.replace(':', '_')}.json")
    print(f"== {m}: {round(d['total'] / len(rs) * 1000)} ms per pair, memory {round((d['mem'] or 0) / 1e9, 1)} GB")
    L = [r for r in rs if r["label"] is not None]
    pos = [r for r in L if r["label"] == 1]
    neg = [r for r in L if r["label"] == 0]
    for k in ("noul", "score"):
        print(f"   {k} AUC {round(auc([r[k] for r in pos], [r[k] for r in neg]), 3)}")
    for th in (0.3, 0.5, 0.7, 0.85):
        print(f"   noul >= {th}:", stats(rs, lambda r: r["noul"] >= th))
    for th in (1.0, 1.5, 2.0):
        print(f"   score >= {th}:", stats(rs, lambda r: r["score"] >= th))
