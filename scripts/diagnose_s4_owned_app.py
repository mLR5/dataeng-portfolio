import json, os, subprocess, time
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
props=app.get('properties',app)
report={'owned_app_exists':True,'state':props.get('state'),'config':props.get('functionAppConfig')}
settings,code=az(['functionapp','config','appsettings','list','--name',APP,'--resource-group',RG])
names=sorted(x['name'] for x in settings or [])
report['setting_names']=names
report['config']=None
required={'AzureWebJobsStorage','DEPLOYMENT_STORAGE_CONNECTION_STRING','SILVER_ROOT','SILVER_TIMER_SCHEDULE','SILVER_HEALTH_SCHEDULE'}
if code==0 and required<=set(names) and 'QUEUE_NAME' not in names:
    _,code=az(['functionapp','config','appsettings','set','--name',APP,'--resource-group',RG,'--settings','QUEUE_NAME=vs4-unused-'+NONCE])
    report['missing_queue_name_repaired']=code==0
    time.sleep(10)
functions,code=az(['functionapp','function','list','--name',APP,'--resource-group',RG])
report['function_names']=sorted(x['name'].rsplit('/',1)[-1] for x in functions or [])
deploys,code=az(['functionapp','log','deployment','list','--name',APP,'--resource-group',RG])
report['deployment_listing_available']=code==0
if isinstance(deploys,list):
    report['deployments']=[{k:x.get(k) for k in ('id','status','complete','active')} for x in deploys[:3]]
print(json.dumps(report))
