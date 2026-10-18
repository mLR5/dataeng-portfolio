"""S1/S2/S3 integration: real pure transformation and real local Delta storage."""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from deltalake import DeltaTable

from shared.claim_check import bronze_blob_name
from shared.silver_contract import TABLE_SCHEMAS, OBSERVATION_TABLE, FORECAST_TABLE
from shared.silver_delta import DeltaSnapshotWriter
from shared.silver_publication import PublicationConflict, published_read_plan
from shared.silver_runner import SilverRunner
from shared.silver_transform import transform_bronze


class ControlStore:
    def __init__(self):
        self.values = {}
    def read(self, key):
        return self.values.get(key)
    def create(self, key, value):
        if key in self.values and self.values[key] != value:
            raise PublicationConflict('Immutable object differs')
        self.values[key] = value


class TestLease:
    __test__ = False
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def assert_held(self):
        pass


def source_bytes(ingestion_id='20261018_100000', *, bad_hour=False, collection_failed=False):
    timestamp = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
    city = {
        'city': 'Paris', 'status': 'success', 'data': {
            'location': {'name':'Paris', 'country':'France', 'lat':48.85,
                         'lon':2.35, 'tz_id':'Europe/Paris'},
            'current': {'last_updated_epoch':int(timestamp.timestamp()), 'temp_c':12.5},
            'forecast': {'forecastday':[{
                'date':'2026-10-18', 'hour':[
                    {'time_epoch':int(timestamp.timestamp()) + 3600, 'temp_c':13.0},
                    {'time_epoch':int(timestamp.timestamp()) + 7200, 'temp_c':14.0,
                     'humidity':120 if bad_hour else 70},
                ],
            }]},
        },
    }
    if collection_failed:
        city.update(status='error', data=None)
    value = {
        'ingestion_id':ingestion_id, 'ingestion_timestamp':timestamp.isoformat(),
        'status':'failed' if collection_failed else 'success', 'cities':[city],
        'summary':{'total_cities':1, 'successful':0 if collection_failed else 1,
                   'failed':1 if collection_failed else 0},
    }
    return json.dumps(value).encode(), bronze_blob_name(ingestion_id, timestamp.isoformat())


def runner_for(tmp_path, raw):
    container = SimpleNamespace(get_blob_client=lambda _: SimpleNamespace(
        download_blob=lambda: SimpleNamespace(readall=lambda: raw)))
    return SilverRunner(container, ControlStore(), DeltaSnapshotWriter(tmp_path),
                        TestLease, transform_bronze)


@pytest.mark.parametrize('bad_hour, expected_status, expected_forecasts', [(False,'success',2), (True,'partial',1)])
def test_real_transform_delta_publication_replay_and_gold(tmp_path, bad_hour, expected_status, expected_forecasts):
    raw, source_blob = source_bytes(bad_hour=bad_hour)
    runner = runner_for(tmp_path, raw)
    result = runner.process_ingestion(source_blob)
    assert result.outcome == 'published'
    assert result.publication['status'] == expected_status
    assert result.publication['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result.publication['quarantine']['count'] == int(bad_hour)
    assert result.publication['tables'][OBSERVATION_TABLE]['row_count'] == 1
    assert result.publication['tables'][FORECAST_TABLE]['row_count'] == expected_forecasts
    plan = published_read_plan([result.publication])
    for name, spec in plan['tables'].items():
        table = DeltaTable(spec['path'], version=spec['version']).to_pyarrow_table(
            filters=[('ingestion_id', 'in', plan['ingestion_ids'])])
        assert table.num_rows == (1 if name == OBSERVATION_TABLE else expected_forecasts)
    assert runner.process_ingestion(source_blob).outcome == 'already_processed'


def test_real_transform_all_failed_publishes_explicit_empty(tmp_path):
    raw, source_blob = source_bytes(collection_failed=True)
    runner = runner_for(tmp_path, raw)
    publication = runner.process_ingestion(source_blob).publication
    assert publication['status'] == 'empty'
    assert publication['summary']['reason'] == 'collection_failed'
    for name in TABLE_SCHEMAS:
        assert DeltaTable(runner.writer.table_path(name)).to_pyarrow_table().num_rows == 0


def test_real_transform_invalid_json_publishes_terminal_rejection(tmp_path):
    _, source_blob = source_bytes()
    runner = runner_for(tmp_path, b'invalid-json')
    result = runner.process_ingestion(source_blob)
    assert result.publication['status'] == 'rejected'
    assert runner.process_ingestion(source_blob).outcome == 'already_processed'


def test_real_transform_mismatching_payload_id_is_terminal_rejected(tmp_path):
    raw, source_blob = source_bytes()
    payload = json.loads(raw)
    payload['ingestion_id'] = '20261018_100001'
    runner = runner_for(tmp_path, json.dumps(payload).encode())
    result = runner.process_ingestion(source_blob)
    assert result.publication['status'] == 'rejected'
    assert result.ingestion_id == '20261018_100000'


@pytest.mark.parametrize('filename', ['bronze_success_20261017_derived_real.json',
                                     'bronze_success_20261018_derived_real.json'])
def test_s1_real_derived_fixtures_transform_publish_and_replay(tmp_path, filename):
    from pathlib import Path
    raw = (Path(__file__).parent / 'fixtures' / 'weatherapi' / filename).read_bytes()
    payload = json.loads(raw)
    source_blob = bronze_blob_name(payload['ingestion_id'], payload['ingestion_timestamp'])
    runner = runner_for(tmp_path, raw)
    first = runner.process_ingestion(source_blob)
    assert first.publication['status'] == 'partial'
    assert first.publication['tables'][OBSERVATION_TABLE]['row_count'] == 2
    assert first.publication['tables'][FORECAST_TABLE]['row_count'] == 12  # six retained hours per valid city
    # The source resolves the query Londres to Argentina; the registered London
    # identity must be rejected rather than published as a British observation.
    actual = DeltaTable(runner.writer.table_path(OBSERVATION_TABLE)).to_pyarrow_table()
    assert set(actual.column('city_id').to_pylist()) == {'fr_paris', 'be_brussels'}
    before = {name:DeltaTable(runner.writer.table_path(name)).version() for name in TABLE_SCHEMAS}
    assert runner.process_ingestion(source_blob).outcome == 'already_processed'
    assert {name:DeltaTable(runner.writer.table_path(name)).version() for name in TABLE_SCHEMAS} == before
