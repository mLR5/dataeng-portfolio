"""Read-only post-merge check of the existing Functions and Silver table state."""
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPORT = {'result': 'failed', 'main_commit': os.environ['VALIDATED_MAIN'], 'checks': []}


def az(args):
    result = subprocess.run(['az', *args, '--only-show-errors', '--output', 'json'],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Read-only Azure CLI check failed; raw output withheld')
    return json.loads(result.stdout)


def check(name, condition):
    REPORT['checks'].append({'name': name, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(name)


def main():
    project = os.environ['PROJECT_NAME']
    environment = os.environ['ENVIRONMENT']
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    args = ['--resource-group', f'rg-{project}-{environment}', '--name', f'func-{project}-{environment}']
    app = az(['functionapp', 'show', *args])
    props = app.get('properties', app)
    check('function_app_running', str(props.get('state', '')).lower() == 'running')
    functions = az(['functionapp', 'function', 'list', *args])
    names = {item['name'].rsplit('/', 1)[-1] for item in functions}
    expected = {'manual_weather_trigger', 'weather_ingestion_trigger',
                'weather_bronze_processor', 'bronze_health_monitor'}
    REPORT['indexed_functions'] = sorted(names)
    check('existing_four_functions_indexed', expected <= names)
    REPORT['silver_timer_indexed'] = any('silver' in name for name in names)
    keys = az(['functionapp', 'keys', 'list', *args])
    state = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            request = Request(f"https://{props['defaultHostName']}/admin/host/status",
                              headers={'x-functions-key': keys['masterKey']})
            with urlopen(request, timeout=20) as response:
                state = json.loads(response.read()).get('state')
            if str(state).lower() == 'running':
                break
        except (HTTPError, URLError, TimeoutError):
            pass
        time.sleep(5)
    keys = None
    REPORT['host_state'] = state
    check('runtime_host_running', str(state).lower() == 'running')
    root = 'silver/weatherapi/schema=v1/transform=1.0.0'
    REPORT['silver_business_tables_exist'] = {}
    for table in ('observation_snapshots', 'forecast_hourly_snapshots'):
        found = az(['storage', 'blob', 'exists', '--account-name', account, '--auth-mode', 'key',
                    '--container-name', 'datalake', '--name', f'{root}/{table}/_delta_log/00000000000000000000.json'])
        REPORT['silver_business_tables_exist'][table] = bool(found['exists'])
    publications = az(['storage', 'blob', 'list', '--account-name', account, '--auth-mode', 'key',
                       '--container-name', 'datalake', '--prefix', root + '/_control/publications/',
                       '--num-results', '10', '--query', '[].name'])
    REPORT['silver_publications_sample_count'] = len(publications)
    REPORT['result'] = 'success'


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        REPORT['error_type'] = type(error).__name__
    finally:
        Path('merged-runtime-report.json').write_text(json.dumps(REPORT, indent=2) + '\n')
        print(json.dumps(REPORT, separators=(',', ':')), flush=True)
    raise SystemExit(0 if REPORT['result'] == 'success' else 1)
