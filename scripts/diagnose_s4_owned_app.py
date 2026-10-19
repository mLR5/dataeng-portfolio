import json, os, subprocess, tempfile
from pathlib import Path
NONCE='39342792976-1'
APP='vs4-'+NONCE
RG='rg-'+os.environ['PROJECT_NAME']+'-'+os.environ['ENVIRONMENT']
def az(args):
    r=subprocess.run(['az',*args,'--output','json','--only-show-errors'],capture_output=True,text=True,timeout=90)
    return (json.loads(r.stdout) if r.returncode==0 and r.stdout.strip() else None),r.returncode
app,code=az(['functionapp','show','--name',APP,'--resource-group',RG])
if code:
    print(json.dumps({'owned_app_exists':False}));raise SystemExit(0)
if (app.get('tags') or {}).get('validation_id')!=NONCE:
    raise RuntimeError('Ownership mismatch')
report={'owned_app_exists':True}
settings,code=az(['functionapp','config','appsettings','list','--name',APP,'--resource-group',RG])
allowed={'BRONZE_CONTAINER','SILVER_ROOT','SILVER_START_AT','SILVER_ENABLED','AzureWebJobs.weather_silver_processor.Disabled'}
report['selected_nonsecret_settings']={x['name']:x['value'] for x in settings or [] if x['name'] in allowed}
for phase in ('bootstrap','resume'):
    report[phase]={}
    for filename in ('coordinator.json','health.json'):
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'data.json'
            name='silver/validation-s4/'+NONCE+'/'+phase+'/_control/runtime/'+filename
            _,code=az(['storage','blob','download','--account-name',os.environ['AZURE_STORAGE_ACCOUNT'],'--auth-mode','key','--container-name','datalake','--name',name,'--file',str(target),'--no-progress'])
            if code:
                report[phase][filename]={'exists':False};continue
            data=json.loads(target.read_bytes())
        if filename=='health.json':
            report[phase][filename]=data
        else:
            report[phase][filename]={key:data.get(key) for key in ('last_started_at','last_finished_at','last_run','latest_source_at','latest_usable_at')}
            report[phase][filename]['pending_count']=len(data['pending'])
            report[phase][filename]['pending_errors']=[{key:job.get(key) for key in ('attempts','last_error_type','next_attempt_at')} for job in list(data['pending'].values())[:3]]
print(json.dumps(report))
