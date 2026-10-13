"""Score nimble-v3 vs old nimble on the labelled pairs of the decision-filter test set.

Usage: python3 -I compare.py <bench_dir> <out_dir>
"""
import json, sys

bench, out = sys.argv[1], sys.argv[2]
ts = json.load(open(f"{bench}/testset.json"))


def rows(path):
    d = json.load(open(path))
    r, i, rs = d["res"], 0, []
    for ti, t in enumerate(ts):
        for rank, c in enumerate(t["cands"]):
            rs.append({**r[i], "label": c["label"], "src": c["src"], "rank": rank, "ti": ti})
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


def show(name, d, rs):
    n = len(rs)
    print(f"== {name}: {round(d['total'] / n * 1000)} ms per pair, memory {round((d['mem'] or 0) / 1e9, 1)} GB")


old_ref = json.load(open(f"{bench}/res_nimble_latest.json"))["res"]
d, rs = rows(f"{out}/res_nimble_latest.json")
show("old nimble (rc1)", d, rs)
print("  ", stats(rs, lambda r: r["keep"]))
same = sum(a["keep"] == b["keep"] for a, b in zip(old_ref, d["res"]))
print(f"   same answers as the 10-09 run on 0.40.x: {same}/{len(old_ref)}")
_, ref = rows(f"{bench}/res_nimble_latest.json")
print("   10-09 run:", stats(ref, lambda r: r["keep"]))

d, rs = rows(f"{out}/res_nimble-v3_latest.json")
show("nimble-v3", d, rs)
L = [r for r in rs if r["label"] is not None]
pos = [r for r in L if r["label"] == 1]
neg = [r for r in L if r["label"] == 0]
for k in ("noul", "score"):
    print(f"   {k} AUC {round(auc([r[k] for r in pos], [r[k] for r in neg]), 3)}")
for th in (0.3, 0.5, 0.7, 0.85):
    print(f"   noul >= {th}:", stats(rs, lambda r: r["noul"] >= th))
for th in (1.0, 1.5, 2.0):
    print(f"   score >= {th}:", stats(rs, lambda r: r["score"] >= th))

_, lr = rows(f"{bench}/res_laya_latest.json")
Ll = [r for r in lr if r["label"] is not None]
print("== laya (10-09) score AUC",
      round(auc([r["score"] for r in Ll if r["label"] == 1], [r["score"] for r in Ll if r["label"] == 0]), 3))
