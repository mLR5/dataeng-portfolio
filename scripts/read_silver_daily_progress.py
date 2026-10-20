import json,os,subprocess
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueServiceClient
from azure.core.exceptions import ResourceNotFoundError
nonce='39355949893-1'
rg=f"rg-{os.environ['PROJECT_NAME']}-{os.environ['ENVIRONMENT']}"
def az(args):
 r=subprocess.run(['az',*args,'--output','json','--only-show-errors'],capture_output=True,text=True)
 return json.loads(r.stdout) if r.returncode==0 else None
app=az(['functionapp','show','-g',rg,'-n',f'vs4-{nonce}'])
print('OWNED_APP_EXISTS',bool(app))
if app:
 if app.get('tags',{}).get('validation_id')!=nonce:raise RuntimeError('Owner mismatch')
 print('APP_STATE',app.get('state') or app.get('properties',{}).get('state'))
 funcs=az(['functionapp','function','list','-g',rg,'-n',f'vs4-{nonce}'])
 print('FUNCTIONS',','.join(f['name'].split('/')[-1] for f in (funcs or [])))
connection=az(['storage','account','show-connection-string','-g',rg,'-n',os.environ['AZURE_STORAGE_ACCOUNT'],'--query','connectionString'])
if not connection:raise RuntimeError('Storage metadata unavailable')
blobs=BlobServiceClient.from_connection_string(connection)
queues=QueueServiceClient.from_connection_string(connection)
for phase in ('bootstrap','resume'):
 try:s=json.loads(blobs.get_blob_client('datalake',f'silver/validation-s4/{nonce}/{phase}/_control/runtime/coordinator.json').download_blob().readall())
 except ResourceNotFoundError:
  print('CHECKPOINT',phase,'absent');continue
 print('CHECKPOINT',phase,'invocations',s.get('invocations'),'target_end',s.get('target_end'),'pending',len(s.get('pending',{})),'finished',s.get('last_finished_at'),'next_wake',s.get('next_wake_at'),'daily_completed',s.get('daily',{}).get('completed_end'),'history_passes',s.get('history',{}).get('passes'))
 last=s.get('last_run') or {}
 print('LAST_RUN',phase,'outcome',last.get('outcome'),'published',last.get('published'),'replay',last.get('already_processed'),'errors',last.get('error_types'),'next_delay',last.get('next_delay_seconds'))
for lane in ('a','b'):
 try:p=queues.get_queue_client(f'vs4-work-{lane}-{nonce}').get_queue_properties();print('QUEUE',lane,'approximate_messages',p.approximate_message_count)
 except ResourceNotFoundError:print('QUEUE',lane,'absent')
