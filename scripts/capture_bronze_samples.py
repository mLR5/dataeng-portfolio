"""Capture two existing public-weather payloads without calling WeatherAPI."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

SAMPLES = {
    'bronze_success_20261017_real.json': 'weather/weatherapi/2026/10/17/23/20261017_230126_82664d4ec7054e89a29dad74b7147012_weather_data.json',
    'bronze_success_20261018_real.json': 'weather/weatherapi/2026/10/18/14/20261018_140443_a832e2104ae947e1a909f1dff8c2a518_weather_data.json',
}


def assert_no_secret(value):
    if isinstance(value, dict):
        forbidden = {'apikey', 'authorization', 'password', 'accesstoken',
                     'storageconnectionstring', 'sastoken', 'connectionstring',
                     'sig', 'secret', 'weatherapikey'}
        for key, item in value.items():
            if re.sub('[^a-z]', '', key.lower()) in forbidden:
                raise RuntimeError('Sensitive field found; refusing artifact')
            assert_no_secret(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_secret(item)
    elif isinstance(value, str):
        if ('AccountKey=' in value or 'SharedAccessSignature=' in value
                or re.search(r'[?&](key|sig|token|api_key)=', value, re.I)):
            raise RuntimeError('Sensitive value found; refusing artifact')


def shape(value, path, result):
    node = result.setdefault(path, {'types': [], 'null_count': 0, 'present_count': 0})
    kind = type(value).__name__
    if kind not in node['types']:
        node['types'].append(kind)
    node['present_count'] += 1
    node['null_count'] += int(value is None)
    if isinstance(value, dict):
        for key, child in value.items():
            shape(child, f'{path}.{key}', result)
    elif isinstance(value, list):
        for child in value:
            shape(child, path + '[]', result)


def main():
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    output = Path('bronze-samples')
    output.mkdir()
    profiles = []
    for filename, source in SAMPLES.items():
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'bronze.json'
            process = subprocess.run([
                'az', 'storage', 'blob', 'download', '--account-name', account,
                '--auth-mode', 'key', '--container-name', 'bronze', '--name', source,
                '--file', str(target), '--only-show-errors', '--output', 'none',
            ], capture_output=True, text=True, timeout=90)
            if process.returncode:
                raise RuntimeError('Read-only Bronze download failed; raw CLI output withheld')
            raw = target.read_bytes()
        if len(raw) > 20_000_000:
            raise RuntimeError('Sample exceeds size bound')
        data = json.loads(raw)
        if data.get('status') != 'success' or len(data.get('cities', [])) != 3:
            raise RuntimeError('Expected the known successful ingestion')
        assert_no_secret(data)
        types = {}
        shape(data, '$', types)
        (output / filename).write_bytes(raw)
        profiles.append({
            'fixture': filename, 'source_blob': source, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'ingestion_id': data['ingestion_id'],
            'ingestion_timestamp': data['ingestion_timestamp'], 'status': data['status'],
            'cities': [{
                'query': city['city'],
                'timezone': city['data']['location'].get('tz_id'),
                'forecast_days': len(city['data']['forecast'].get('forecastday', [])),
                'hours_per_day': [len(day.get('hour', [])) for day in city['data']['forecast'].get('forecastday', [])],
            } for city in data['cities']],
            'shape': types,
        })
        print(f'Captured {filename}: {len(raw)} bytes; 3 successful cities; no new API request', flush=True)
    (output / 'profiles.json').write_text(json.dumps(profiles, indent=2) + '\n')


if __name__ == '__main__':
    main()
