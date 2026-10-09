import json,glob
ts=json.load(open('testset.json'))
def load(m):
    d=json.load(open(f'res_{m}.json'));r=d['res']
    i=0;rows=[]
    for ti,t in enumerate(ts):
        for rank,c in enumerate(t['cands']):
            x=r[i];i+=1;rows.append({**x,'label':c['label'],'src':c['src'],'ti':ti,'rank':rank})
    return d,rows
def auc(p,n):
    return sum((a>b)+0.5*(a==b) for a in p for b in n)/(len(p)*len(n))
def stats(rows,keepf):
    L=[r for r in rows if r['label'] is not None]
    pos=[r for r in L if r['label']==1];neg=[r for r in L if r['label']==0]
    kp=sum(keepf(r) for r in pos);kn=sum(keepf(r) for r in neg)
    prec=kp/(kp+kn) if kp+kn else 0
    emb=[r for r in rows if r['src']=='emb' and r['rank']<8]
    mm=[r for r in rows if r['src']=='mismatch']
    return dict(prec=round(prec,2),pos_dropped=f"{len(pos)-kp}/{len(pos)}",neg_kept=f"{kn}/{len(neg)}",
      mismatch_kept=f"{sum(keepf(r) for r in mm)}/{len(mm)}",top8_keep_rate=round(sum(keepf(r) for r in emb)/len(emb),2))
print('baseline keep-all prec',39/120)
for m in ['laya_latest','laya_322m-multilingual-mlx-fp16','nimble_latest','gemma4_12b-mlx']:
    d,rows=load(m);n=len(rows);lat=d['total']/n
    L=[r for r in rows if r['label'] is not None];pos=[r for r in L if r['label']==1];neg=[r for r in L if r['label']==0]
    print('==',m,'ms/pair',round(lat*1000),'mem GB',round(d['mem']/1e9,1))
    if 'score' in rows[0]:
        for k in ('score','noul'):
            print(k,'AUC',round(auc([r[k] for r in pos],[r[k] for r in neg]),3))
            vals=sorted(set(round(r[k],2) for r in rows))
            import numpy as np
            for th in np.quantile([r[k] for r in rows],[.2,.35,.5,.65]):
                print(' th %.3f'%th,stats(rows,lambda r:r[k]>=th))
        # rerank: rank of positive among first-8 emb cands
        for k in ('score','noul'):
            rr=[];er=[]
            for ti,t in enumerate(ts):
                cs=[r for r in rows if r['ti']==ti and r['src']=='emb' and r['rank']<8]
                for p in [r for r in cs if r['label']==1]:
                    er.append(p['rank']+1);rr.append(1+sum(o[k]>p[k] for o in cs))
            print(k,'positive mean rank emb',round(sum(er)/len(er),2),'model',round(sum(rr)/len(rr),2),'top3 emb',sum(x<=3 for x in er),'model',sum(x<=3 for x in rr),'of',len(er))
    else:
        print(stats(rows,lambda r:r['keep']))
