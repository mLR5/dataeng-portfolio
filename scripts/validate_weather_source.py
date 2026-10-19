"""Validate live source queries and Silver in memory; no storage or queue writes."""
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))

from shared import config
from shared.claim_check import bronze_blob_name
from shared.silver_cities import resolve_city
from shared.silver_transform import transform_bronze
from shared.weather_api import get_all_cities_data

REPORT = {
    'result': 'failed',
    'validated_commit': os.environ['VALIDATED_COMMIT'],
    'scope': 'live API -> in-memory collector envelope -> local Silver; no storage or queue writes',
    'checks': [],
    'cities': [],
}


def check(name, condition):
    REPORT['checks'].append({'name': name, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(name)


def api_key():
    project = os.environ['PROJECT_NAME']
    environment = os.environ['ENVIRONMENT']
    result = subprocess.run(
        ['az', 'keyvault', 'secret', 'show', '--vault-name', f'kv-{project}-{environment}',
         '--name', 'weather-api-key', '--query', 'value', '--output', 'json', '--only-show-errors'],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        for code in ('ForbiddenByRbac', 'Forbidden', 'SecretNotFound', 'Unauthorized', 'ForbiddenByFirewall'):
            if code in result.stderr:
                REPORT['azure_error_code'] = code
                break
        raise RuntimeError('Cannot read existing WeatherAPI credential; raw CLI output withheld')
    key = json.loads(result.stdout)
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError('WeatherAPI credential is empty')
    return key


def main():
    REPORT['stage'] = 'read_existing_key_vault_secret'
    key = api_key()
    REPORT['stage'] = 'collect_live_weatherapi'
    data = get_all_cities_data(key)
    key = None
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
    raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
    # Canonical path used only to exercise the transform contract; no blob is created.
    path = bronze_blob_name(data['ingestion_id'], data['ingestion_timestamp'])
    transformed = transform_bronze(raw, path)
    check('silver_accepts_all_cities_without_rejections', transformed.status == 'success' and not transformed.rejections)
    check('three_observations', transformed.observations.num_rows == 3)
    check('all_forecast_hours_preserved', transformed.forecasts.num_rows == forecast_count)
    check('london_present_as_gb_london', any(row['city_id'] == 'gb_london' for row in transformed.observations.to_pylist()))
    REPORT['silver'] = {'status': transformed.status, 'observations': transformed.observations.num_rows,
                        'forecasts': transformed.forecasts.num_rows, 'rejections': len(transformed.rejections)}
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
