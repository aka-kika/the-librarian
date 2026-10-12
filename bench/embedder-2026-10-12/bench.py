import json, sqlite3, struct, sys, time, math, httpx
import numpy as np
DB = "/Users/kika_hub/.skill_librarian/librarian.db"
TS = "/Users/kika_hub/_KIKA_MAIN/_INFRA/mcp/servers/the_librarian/bench/decision-filter-2026-10-09/testset.json"
NEW = "embeddinggemma-2:740m-mxfp8"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = conn.execute("SELECT s.name, s.description, s.body, e.dim, e.vec FROM skills s JOIN embeddings e ON e.skill_id=s.id").fetchall()
names = [r[0] for r in rows]
old = np.array([struct.unpack(f"{r[3]}f", r[4]) for r in rows])
def emb(texts, model):
    out = []
    for i in range(0, len(texts), 16):
        r = httpx.post("http://localhost:11434/api/embed", json={"model": model, "input": texts[i:i+16], "truncate": True}, timeout=300)
        r.raise_for_status(); out += r.json()["embeddings"]
    return np.array(out)
surface = [f"{n}\n{d}\n{b[:4000]}"[:8000] for n, d, b, _, _ in rows]
ts = json.load(open(TS))
qs = [t["q"] for t in ts]
variants = {}
t0 = time.time()
variants["gemma plain"] = (emb(surface, NEW), emb(qs, NEW))
print("gemma plain embed s", round(time.time()-t0,1), file=sys.stderr)
t0 = time.time()
variants["gemma prompts"] = (emb([f"title: {r[0]} | text: {s}" for r, s in zip(rows, surface)], NEW),
                             emb([f"task: search result | query: {q}" for q in qs], NEW))
print("gemma prompts embed s", round(time.time()-t0,1), file=sys.stderr)
variants = {"nomic (live)": (old, emb(qs, "nomic-embed-text")), **variants}
def norm(m): return m / np.linalg.norm(m, axis=1, keepdims=True)
res = {}
for v, (D, Q) in variants.items():
    S = norm(Q) @ norm(D).T
    ranks, h5, h10, auc_n, auc_d = [], 0, 0, 0, 0
    for i, t in enumerate(ts):
        order = list(np.argsort(-S[i]))
        pos = [c["skill"] for c in t["cands"] if c["label"] == 1 and c["skill"] in names]
        neg = [c["skill"] for c in t["cands"] if c["label"] == 0 and c["skill"] in names]
        if pos:
            r = min(order.index(names.index(p)) for p in pos) + 1
            ranks.append(r); h5 += r <= 5; h10 += r <= 10
        for p in pos:
            for n in neg:
                auc_d += 1; auc_n += S[i][names.index(p)] > S[i][names.index(n)]
    n = len(ranks)
    res[v] = dict(intents=n, hit5=f"{h5}/{n}", hit10=f"{h10}/{n}", mrr=round(sum(1/r for r in ranks)/n, 3),
                  median_rank=int(np.median(ranks)), good_above_bad=f"{auc_n}/{auc_d}")
print(json.dumps(res, indent=1))
# fairness check: how many top-5 picks were never judged (labels came from nomic's own results)
import random
blind = []
for v in ["nomic (live)", "gemma prompts"]:
    D, Q = variants[v]; S = norm(Q) @ norm(D).T
    un = 0
    for i, t in enumerate(ts):
        lab = {c["skill"] for c in t["cands"] if c["label"] is not None}
        for j in np.argsort(-S[i])[:5]:
            if names[j] not in lab:
                un += 1; blind.append((i, names[j]))
    print(v, "unjudged in top5:", un, "of", 5*len(ts))
desc = {r[0]: r[1] for r in rows}
items = sorted(set(blind)); random.seed(7); random.shuffle(items)
json.dump([{"id": k, "q": qs[i], "skill": s, "desc": desc[s][:400]} for k, (i, s) in enumerate(items)], open("blind.json", "w"), indent=1)
json.dump({"Q": {v: variants[v][1].tolist() for v in variants}}, open("qvec.json", "w"))
np.save("gemma_prompts_D.npy", variants["gemma prompts"][0]); np.save("gemma_plain_D.npy", variants["gemma plain"][0])
print("blind items:", len(items))
