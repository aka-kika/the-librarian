import urllib.request,json,time
def post(path,d):
    r=urllib.request.Request('http://127.0.0.1:11434'+path,json.dumps(d).encode(),{'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(r,timeout=300))
def gen(m,p,**o):
    t=time.time();r=post('/api/generate',{'model':m,'prompt':p,'stream':False,'think':False,'options':{'temperature':0,'num_predict':8,**o}});return r.get('response'),time.time()-t
