"""Read-only inventory of project infrastructure. Never serialize credentials."""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from itertools import islice
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueServiceClient


def az(*args):
    result = subprocess.run(['az', *args, '--output', 'json', '--only-show-errors'], capture_output=True, text=True)
    if result.returncode:
        code = re.search(r'ERROR: \(([A-Za-z0-9_]+)\)', result.stderr)
        raise RuntimeError((code.group(1) if code else 'read_failed') + ': ' + ' '.join(args[:2]))
    return json.loads(result.stdout or 'null')


project = os.environ['PROJECT_NAME']
environment = os.environ['ENVIRONMENT']
account = os.environ['AZURE_STORAGE_ACCOUNT']
group = f'rg-{project}-{environment}'
report = {'observed_at_utc': datetime.now(timezone.utc).isoformat(), 'read_only': True, 'resource_group': group, 'resources': [], 'linked_resources': [], 'containers': [], 'queues': [], 'functions': [], 'alerts': [], 'storage_checks': {}, 'errors': []}
resources = az('resource', 'list', '--resource-group', group)

for resource in sorted(resources, key=lambda r: (r['type'].lower(), r['name'])):
    item = {k: resource.get(k) for k in ('name', 'type', 'location')}
    tags = resource.get('tags') or {}
    item['tags'] = {k: v for k, v in tags.items() if k.lower() in {'project', 'environment', 'purpose', 'validation_id', 'owner', 'managed_by', 'managedby'}}
    report['resources'].append(item)
    kind = resource['type'].lower()
    if kind == 'microsoft.web/sites':
        details = az('functionapp', 'show', '--name', resource['name'], '--resource-group', group)
        item.update(state=details.get('state'), kind=details.get('kind'), runtime=details.get('functionAppConfig', {}).get('runtime'))
        funcs = az('functionapp', 'function', 'list', '--name', resource['name'], '--resource-group', group)
        report['functions'].append({'app': resource['name'], 'names': sorted(f['name'].split('/')[-1] for f in funcs)})
    elif kind == 'microsoft.web/serverfarms':
        details = az('resource', 'show', '--ids', resource['id'])
        item['sku'] = details.get('sku')
    elif kind == 'microsoft.insights/components':
        details = az('resource', 'show', '--ids', resource['id'], '--api-version', '2020-02-02')
        workspace = details.get('properties', {}).get('WorkspaceResourceId')
        if workspace:
            try:
                linked = az('resource', 'show', '--ids', workspace, '--api-version', '2023-09-01')
                report['linked_resources'].append({k: linked.get(k) for k in ('name', 'type', 'location', 'resourceGroup')})
            except RuntimeError as exc:
                parts = workspace.split('/')
                report['linked_resources'].append({'name': parts[-1], 'type': '/'.join(parts[-3:-1]), 'resourceGroup': parts[4], 'details_readable': False, 'read_error': str(exc)})
    elif kind == 'microsoft.insights/scheduledqueryrules':
        details = az('resource', 'show', '--ids', resource['id'], '--api-version', '2023-12-01')
        props = details.get('properties', {})
        report['alerts'].append({'name': resource['name'], 'enabled': props.get('enabled'), 'severity': props.get('severity'), 'action_group_count': len(props.get('actions', {}).get('actionGroups', [])), 'evaluation_frequency': props.get('evaluationFrequency')})
    elif kind == 'microsoft.storage/storageaccounts':
        details = az('storage', 'account', 'show', '--name', resource['name'], '--resource-group', group)
        item.update(kind=details.get('kind'), sku=details.get('sku', {}).get('name'), hns_enabled=details.get('isHnsEnabled'))

# Credentials stay in memory: they are never printed or added to the report.
connection = az('storage', 'account', 'show-connection-string', '--name', account, '--resource-group', group)['connectionString']
blobs = BlobServiceClient.from_connection_string(connection)
queues = QueueServiceClient.from_connection_string(connection)
containers = list(blobs.list_containers())
for container in sorted(containers, key=lambda c: c['name']):
    report['containers'].append({'name': container['name'], 'public_access': container.get('public_access')})
for queue in sorted(queues.list_queues(), key=lambda q: q['name']):
    properties = queues.get_queue_client(queue['name']).get_queue_properties()
    report['queues'].append({'name': queue['name'], 'approximate_messages': properties.approximate_message_count})
names = {c['name'] for c in containers}
if 'datalake' in names:
    lake = blobs.get_container_client('datalake')
    root = 'silver/weatherapi/schema=v1/transform=1.0.0'
    for label, suffix in [('coordinator', '_control/runtime/coordinator.json'), ('observation_delta_initial_commit', 'observation_snapshots/_delta_log/00000000000000000000.json'), ('forecast_delta_initial_commit', 'forecast_hourly_snapshots/_delta_log/00000000000000000000.json')]:
        report['storage_checks'][label] = lake.get_blob_client(f'{root}/{suffix}').exists()
    for prefix in ['silver/validation-s4/', 'silver/validation-', 'silver/validation/']:
        found = list(islice(lake.list_blobs(name_starts_with=prefix), 101))
        report['storage_checks'][prefix] = {'bounded_count': len(found), 'truncated': len(found) > 100, 'sample_names': [b.name for b in found[:15]]}
    report['storage_checks']['datalake_root_entries'] = [b.name for b in islice(lake.walk_blobs(delimiter='/'), 30)]
if 'deploymentpackage' in names:
    report['storage_checks']['deployment_packages'] = [{'name': b.name, 'bytes': b.size} for b in islice(blobs.get_container_client('deploymentpackage').list_blobs(), 12)]
if 'azure-webjobs-hosts' in names:
    host = blobs.get_container_client('azure-webjobs-hosts')
    for prefix in ['locks/vs4-', 'timers/vs4-']:
        found = list(islice(host.list_blobs(name_starts_with=prefix), 101))
        report['storage_checks'][prefix] = {'bounded_count': len(found), 'truncated': len(found) > 100, 'sample_names': [b.name for b in found[:10]]}
report['temporary_validation_resources'] = [r for r in report['resources'] if r['name'].startswith(('vs4-', 'asp-vs4-')) or r['tags'].get('validation_id')]
report['temporary_validation_containers'] = [c for c in report['containers'] if c['name'].startswith('vs4-')]
Path('azure-resource-inventory.json').write_text(json.dumps(report, indent=2, default=str) + '\n')
print('READ_ONLY_AZURE_INVENTORY=' + json.dumps(report, separators=(',', ':'), default=str))
