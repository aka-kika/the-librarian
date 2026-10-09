import sys,json,time
from oll import *
m=sys.argv[1]; ts=json.load(open('testset.json'))
INS="Does this skill help accomplish the user's intent? Judge only from the skill description."
def laya(q,n,d,mx):
    st=f"Intent: {q[:300]}\nSkill: {n}: {d[:mx]}"
    r=post('/v1/systemone',{'model':m,'state':st,'questions':{
      'fit':{'type':'score','instructions':INS,'criteria':['unrelated','weak','good','perfect']},
      'rel':{'type':'noul','instructions':INS}}})
    a=r['answers'];return {'score':a['fit']['score'],'noul':a['rel']['noul']}
def nimble(q,n,d,mx):
    p=f"""Schema:
field: fit
meaning: Does the skill help accomplish the user's intent? Judge only from the skill description.
choices:
A: yes, the skill is directly useful for this task
B: no, the skill is unrelated, too generic, or for a different task

Context:
Intent: {q}
Skill: {n} - {d[:mx]}

Field: fit
Answer:"""
    o,_=gen(m,p,num_predict=3);return {'keep':o.strip().upper().startswith('A')}
def gemma(q,n,d,mx):
    p=f"""You judge whether a skill fits a task. Intent: {q}
Skill: {n} - {d[:mx]}
Would using this skill directly help accomplish the intent? Answer with one word: YES or NO."""
    o,_=gen(m,p,num_predict=4);return {'keep':o.strip().upper().startswith('Y')}
f={'laya':laya,'nimble':nimble,'gemma':gemma}[m.split(':')[0].replace('4','').replace('laya','laya')] if False else (laya if m.startswith('laya') else nimble if m.startswith('nimble') else gemma)
mx=250 if m.startswith('laya') else 600
f(ts[0]['q'],'x','y',mx)  # warm
res=[];t0=time.time()
for ti,t in enumerate(ts):
    for c in t['cands']:
        t1=time.time();r=f(t['q'],c['skill'],c['desc'],mx);r['t']=time.time()-t1;r['ti']=ti;r['skill']=c['skill'];res.append(r)
tot=time.time()-t0
ps=[x for x in json.load(urllib.request.urlopen('http://127.0.0.1:11434/api/ps'))['models'] if x['name']==m]
json.dump({'res':res,'total':tot,'mem':ps[0]['size'] if ps else None},open('res_'+m.replace(':','_')+'.json','w'))
print(m,len(res),round(tot,1),ps[0]['size']/1e9 if ps else None)
