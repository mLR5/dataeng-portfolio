import json, os, subprocess, tempfile
from pathlib import Path
NONCE='39342792976-1'
ACCOUNT=os.environ['AZURE_STORAGE_ACCOUNT']
def az(args):
    r=subprocess.run(['az',*args,'--output','json','--only-show-errors'],capture_output=True,text=True,timeout=90)
    return (json.loads(r.stdout) if r.returncode==0 and r.stdout.strip() else None),r.returncode
def blob(name):
    with tempfile.TemporaryDirectory() as temporary:
        target=Path(temporary)/'data.json'
        _,code=az(['storage','blob','download','--account-name',ACCOUNT,'--auth-mode','key','--container-name','datalake','--name',name,'--file',str(target),'--no-progress'])
        return json.loads(target.read_bytes()) if code==0 else None
root='silver/validation-s4/'+NONCE
owner=blob(root+'/owner.json')
if owner is None:
    print(json.dumps({'owned_prefix_present':False}));raise SystemExit(0)
if owner!={'owner':NONCE}:
    raise RuntimeError('Ownership mismatch')
report={'owned_prefix_present':True}
for phase in ('bootstrap','resume'):
    report[phase]={}
    for filename in ('coordinator.json','health.json'):
        data=blob(root+'/'+phase+'/_control/runtime/'+filename)
        if data is None:
            report[phase][filename]={'exists':False};continue
        if filename=='health.json':
            report[phase][filename]=data
        else:
            report[phase][filename]={key:data.get(key) for key in ('last_started_at','last_finished_at','last_run','latest_source_at','latest_usable_at')}
            report[phase][filename]['pending_count']=len(data['pending'])
            report[phase][filename]['pending_errors']=[{key:job.get(key) for key in ('attempts','last_error_type','next_attempt_at')} for job in list(data['pending'].values())[:3]]
print(json.dumps(report))
