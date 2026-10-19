"""One normal deployed ingestion, then read-only Bronze and local Silver checks."""
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time as clock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))

from shared import config
from shared.claim_check import bronze_blob_name
from shared.silver_cities import resolve_city
from shared.silver_transform import transform_bronze

REPORT = {
    'result': 'failed',
    'validated_commit': os.environ['VALIDATED_COMMIT'],
    'scope': 'one normal deployed ingestion -> actual Bronze archive -> local Silver; no Delta writes',
    'checks': [],
    'cities': [],
}


def check(name, condition):
    REPORT['checks'].append({'name': name, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(name)


def az(args):
    result = subprocess.run(['az', *args, '--only-show-errors', '--output', 'json'],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Azure CLI check failed; raw output withheld')
    return json.loads(result.stdout)


def deployed_bronze():
    project = os.environ['PROJECT_NAME']
    environment = os.environ['ENVIRONMENT']
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    args = ['--resource-group', f'rg-{project}-{environment}', '--name', f'func-{project}-{environment}']
    app = az(['functionapp', 'show', *args])
    props = app.get('properties', app)
    check('function_app_running', str(props.get('state', '')).lower() == 'running')
    names = {entry['name'].rsplit('/', 1)[-1] for entry in az(['functionapp', 'function', 'list', *args])}
    expected = {'manual_weather_trigger', 'weather_ingestion_trigger', 'weather_bronze_processor', 'bronze_health_monitor'}
    check('four_existing_functions_indexed', expected <= names)
    keys = az(['functionapp', 'keys', 'list', *args])
    headers = {'x-functions-key': keys['masterKey'], 'Content-Type': 'application/json'}
    host = props['defaultHostName']
    deadline = clock.monotonic() + 90
    running = False
    while clock.monotonic() < deadline:
        try:
            with urlopen(Request(f'https://{host}/admin/host/status', headers=headers), timeout=20) as result:
                running = str(json.loads(result.read()).get('state', '')).lower() == 'running'
            if running:
                break
        except (HTTPError, URLError, TimeoutError):
            pass
        clock.sleep(5)
    check('runtime_host_running', running)
    REPORT['stage'] = 'invoke_one_normal_ingestion'
    # Exactly one POST. Retrying could create another collection after a timeout.
    request = Request(f'https://{host}/api/weather/ingest', data=b'{}', method='POST', headers=headers)
    try:
        with urlopen(request, timeout=180) as result:
            REPORT['http_status'] = result.status
            summary = json.loads(result.read())
    except HTTPError as error:
        REPORT['http_status'] = error.code
        summary = json.loads(error.read())
    finally:
        keys = headers = None
    if isinstance(summary.get('ingestion_id'), str):
        REPORT['ingestion_id'] = summary['ingestion_id']
    check('manual_collection_success', REPORT['http_status'] == 200 and summary.get('status') == 'success'
          and summary.get('cities_retrieved') == 3 and summary.get('cities_failed') == 0)
    ingestion_id = summary.get('ingestion_id', '')
    check('ingestion_identity', isinstance(ingestion_id, str) and bool(re.fullmatch(r'\d{8}_\d{6}_[0-9a-f]{32}', ingestion_id)))
    check('queue_message_created', bool(summary.get('queue_message_id')))
    REPORT['ingestion_id'] = ingestion_id
    print(json.dumps({'ingestion_id': ingestion_id, 'stage': 'waiting_for_actual_bronze'}), flush=True)
    stamp = datetime.strptime(ingestion_id[:15], '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)
    path = f'weather/weatherapi/{stamp:%Y/%m/%d/%H}/{ingestion_id}_weather_data.json'
    REPORT['bronze_blob'] = path
    storage = ['--account-name', account, '--auth-mode', 'key']
    def exists(container, name):
        return az(['storage', 'blob', 'exists', *storage, '--container-name', container, '--name', name])['exists']
    deadline = clock.monotonic() + 180
    found = False
    REPORT['stage'] = 'read_actual_bronze_archive'
    while clock.monotonic() < deadline:
        if exists('bronze', path):
            found = True
            break
        clock.sleep(5)
    check('actual_bronze_archive_created', found)
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / 'bronze.json'
        az(['storage', 'blob', 'download', *storage, '--container-name', 'bronze', '--name', path,
            '--file', str(target), '--overwrite', 'true', '--no-progress'])
        raw = target.read_bytes()
    data = json.loads(raw)
    check('bronze_matches_triggered_ingestion', data.get('ingestion_id') == ingestion_id
          and bronze_blob_name(data['ingestion_id'], data['ingestion_timestamp']) == path)
    deadline = clock.monotonic() + 60
    cleaned = False
    while clock.monotonic() < deadline:
        if not exists('staging', f'raw-ingestion/{ingestion_id}.json'):
            cleaned = True
            break
        clock.sleep(5)
    check('staging_cleaned_by_consumer', cleaned)
    return data, raw, path


def main():
    REPORT['stage'] = 'read_deployed_runtime'
    data, raw, path = deployed_bronze()
    check('all_three_cities_collected', data['status'] == 'success' and data['summary'] == {
        'total_cities': 3, 'successful': 3, 'failed': 0})
    check('three_local_dates_requested', config.DEFAULT_FORECAST_DAYS == 3 and data['forecast_request'] == {'days': 3})
    check('stable_city_labels', [item['city'] for item in data['cities']] == ['Paris', 'Londres', 'Bruxelles'])
    expected_ids = {'Paris': 'fr_paris', 'Londres': 'gb_london', 'Bruxelles': 'be_brussels'}
    forecast_count = 0
    REPORT['stage'] = 'validate_live_geography_and_coverage'
    for entry in data['cities']:
        label = entry['city']
        payload = entry['data']
        location = payload['location']
        city = resolve_city(label, location)
        check('expected_geography_' + city.city_id, city.city_id == expected_ids[label])
        days = payload['forecast']['forecastday']
        dates = [date.fromisoformat(day['date']) for day in days]
        check('three_consecutive_dates_' + city.city_id,
              len(dates) == 3 and dates == [dates[0] + timedelta(days=i) for i in range(3)])
        zone = ZoneInfo(city.timezone)
        provider_today = datetime.fromtimestamp(location['localtime_epoch'], zone).date()
        check('current_local_date_included_' + city.city_id, dates[0] == provider_today)
        counts = []
        for day, local_date in zip(days, dates):
            first = datetime.combine(local_date, time.min, zone).astimezone(timezone.utc)
            last = datetime.combine(local_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
            expected_epochs = set(range(int(first.timestamp()), int(last.timestamp()), 3600))
            hours = day['hour']
            check('complete_local_day_' + city.city_id + '_' + local_date.isoformat(),
                  len(hours) == len(expected_epochs) and {hour['time_epoch'] for hour in hours} == expected_epochs)
            counts.append(len(hours))
            forecast_count += len(hours)
        REPORT['cities'].append({
            'label': label, 'query': config.CITY_QUERIES[label], 'city_id': city.city_id,
            'provider_name': location.get('name'), 'country': location['country'],
            'timezone': location['tz_id'], 'latitude': location['lat'], 'longitude': location['lon'],
            'received_dates': [d.isoformat() for d in dates], 'hours_per_local_day': counts,
        })
    REPORT['stage'] = 'transform_live_envelope_in_memory'
    transformed = transform_bronze(raw, path)
    check('silver_accepts_all_cities_without_rejections', transformed.status == 'success' and not transformed.rejections)
    check('three_observations', transformed.observations.num_rows == 3)
    check('all_forecast_hours_preserved', transformed.forecasts.num_rows == forecast_count)
    check('london_present_as_gb_london', any(row['city_id'] == 'gb_london' for row in transformed.observations.to_pylist()))
    REPORT['silver'] = {'status': transformed.status, 'observations': transformed.observations.num_rows,
                        'forecasts': transformed.forecasts.num_rows, 'rejections': len(transformed.rejections)}
    REPORT['bronze_sha256'] = transformed.source_sha256
    REPORT['result'] = 'success'
    REPORT['stage'] = 'complete'


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        REPORT['error_type'] = type(error).__name__
    finally:
        Path('weather-source-report.json').write_text(json.dumps(REPORT, indent=2) + '\n')
        print(json.dumps(REPORT, separators=(',', ':')), flush=True)
    raise SystemExit(0 if REPORT['result'] == 'success' else 1)
