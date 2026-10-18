import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

import bronze_health_monitor
from shared import claim_check
from shared.bronze_health import check_bronze_health

NOW = datetime(2026, 10, 17, 15, tzinfo=timezone.utc)


@pytest.fixture
def health():
    blobs = Mock()
    container = blobs.get_container_client.return_value
    payloads = {}
    container.list_blobs.side_effect = lambda name_starts_with: [
        SimpleNamespace(name=name) for name in payloads if name.startswith(name_starts_with)
    ]
    container.get_blob_client.side_effect = lambda name: SimpleNamespace(
        download_blob=lambda: SimpleNamespace(readall=lambda: json.dumps(payloads[name]).encode()),
    )
    poison = Mock()
    poison.get_queue_properties.return_value.approximate_message_count = 0

    def add(hour, minute=0, successful=3, failed=0, timestamp=None):
        ingestion_id = f'20261017_{hour:02}{minute:02}00_' + 'b' * 32
        timestamp = timestamp or f'2026-10-17T{hour:02}:{minute:02}:00+00:00'
        name = claim_check.bronze_blob_name(ingestion_id, timestamp)
        payloads[name] = {
            'ingestion_id': ingestion_id, 'ingestion_timestamp': timestamp,
            'status': 'failed' if not successful else ('partial' if failed else 'success'),
            'summary': {'total_cities': successful + failed, 'successful': successful, 'failed': failed},
        }
        return payloads[name]
    return SimpleNamespace(blobs=blobs, container=container, poison=poison, payloads=payloads, add=add)


def test_fresh_bronze_is_detected_from_payload_timestamp(health):
    health.add(14)
    result = check_bronze_health(health.blobs, health.poison, now=NOW)
    assert result['freshness'] == 'fresh'
    assert result['latest_ingestion_at'] == '2026-10-17T14:00:00+00:00'
    assert result['latest_status'] == 'success'
    assert result['poison_messages'] == 0
    assert health.container.list_blobs.call_count == 4


def test_only_recent_hour_partitions_are_scanned_across_midnight(health):
    now = datetime(2026, 10, 18, 0, 15, tzinfo=timezone.utc)
    check_bronze_health(health.blobs, health.poison, now=now)
    prefixes = [invocation.kwargs['name_starts_with'] for invocation in health.container.list_blobs.call_args_list]
    assert prefixes == [f'weather/weatherapi/2026/10/{day}/{hour}/' for day, hour in [('18', '00'), ('17', '23'), ('17', '22'), ('17', '21')]]


def test_health_partition_scan_uses_utc_even_when_caller_clock_has_offset(health):
    health.add(14)
    now = datetime(2026, 10, 17, 17, tzinfo=timezone(timedelta(hours=2)))
    assert check_bronze_health(health.blobs, health.poison, now=now)['freshness'] == 'fresh'
    assert health.container.list_blobs.call_args_list[0].kwargs['name_starts_with'].endswith('/15/')


def test_failed_collection_does_not_reset_business_freshness(health):
    health.add(12)
    health.add(14, successful=0, failed=3)
    result = check_bronze_health(health.blobs, health.poison, now=NOW)
    assert result['freshness'] == 'stale'
    assert result['latest_ingestion_at'] == '2026-10-17T12:00:00+00:00'


def test_partial_is_fresh_but_reported_as_partial(health):
    health.add(14, successful=2, failed=1)
    result = check_bronze_health(health.blobs, health.poison, now=NOW)
    assert (result['freshness'], result['latest_status']) == ('fresh', 'partial')


def test_no_bronze_is_stale_not_healthy(health):
    result = check_bronze_health(health.blobs, health.poison, now=NOW)
    assert result['freshness'] == 'stale'
    assert result['latest_ingestion_at'] is None


def test_freshness_threshold_is_inclusive(health):
    health.add(12, 30)
    assert check_bronze_health(health.blobs, health.poison, now=NOW)['freshness'] == 'fresh'


def test_poison_count_is_measured(health):
    health.add(14)
    health.poison.get_queue_properties.return_value.approximate_message_count = 2
    assert check_bronze_health(health.blobs, health.poison, now=NOW)['poison_messages'] == 2


def test_absent_poison_queue_is_normal_until_first_incident(health):
    health.add(14)
    health.poison.get_queue_properties.side_effect = ResourceNotFoundError('queue missing')
    result = check_bronze_health(health.blobs, health.poison, now=NOW)
    assert result['freshness'] == 'fresh'
    assert result['poison_messages'] == 0
    assert result['poison_queue_exists'] is False


def test_poison_access_error_is_not_converted_to_zero(health):
    health.poison.get_queue_properties.side_effect = HttpResponseError('access denied')
    with pytest.raises(HttpResponseError):
        check_bronze_health(health.blobs, health.poison, now=NOW)


def test_recent_corrupt_bronze_is_an_error_not_a_healthy_signal(health):
    data = health.add(14)
    data['summary']['successful'] = '3'
    with pytest.raises(ValueError, match='summary'):
        check_bronze_health(health.blobs, health.poison, now=NOW)


def test_future_timestamp_is_rejected(health):
    health.add(15, 30)
    with pytest.raises(ValueError, match='future'):
        check_bronze_health(health.blobs, health.poison, now=NOW)


def test_collection_started_during_check_is_allowed_within_clock_tolerance(health):
    health.add(15, timestamp='2026-10-17T15:00:30+00:00')
    assert check_bronze_health(health.blobs, health.poison, now=NOW)['freshness'] == 'fresh'


def test_future_timestamp_outside_clock_tolerance_is_rejected(health):
    health.add(15, timestamp='2026-10-17T15:01:01+00:00')
    with pytest.raises(ValueError, match='future'):
        check_bronze_health(health.blobs, health.poison, now=NOW)


def test_wrong_partition_is_rejected(health):
    health.add(14)['ingestion_timestamp'] = '2026-10-17T13:00:00+00:00'
    with pytest.raises(ValueError, match='partition'):
        check_bronze_health(health.blobs, health.poison, now=NOW)


def test_old_payload_status_can_be_derived_from_summary(health):
    del health.add(14)['status']
    assert check_bronze_health(health.blobs, health.poison, now=NOW)['freshness'] == 'fresh'


def test_monitor_stops_on_oversized_partition(health):
    health.container.list_blobs.side_effect = None
    health.container.list_blobs.return_value = [SimpleNamespace(name=str(i)) for i in range(101)]
    with pytest.raises(RuntimeError, match='scan limit'):
        check_bronze_health(health.blobs, health.poison, now=NOW)
    health.container.get_blob_client.assert_not_called()


def test_timer_emits_parseable_health_event(monkeypatch, caplog):
    monkeypatch.setattr(bronze_health_monitor.BlobServiceClient, 'from_connection_string', Mock())
    monkeypatch.setattr(bronze_health_monitor.QueueClient, 'from_connection_string', Mock())
    event = {'schema_version': 1, 'freshness': 'fresh', 'poison_messages': 0}
    monkeypatch.setattr(bronze_health_monitor, 'check_bronze_health', Mock(return_value=event))
    with caplog.at_level('INFO'):
        bronze_health_monitor.main(Mock())
    message = next(record.message for record in caplog.records if record.message.startswith('BRONZE_HEALTH '))
    assert json.loads(message[14:]) == event


def test_timer_emits_error_without_secret_sdk_details(monkeypatch, caplog):
    monkeypatch.setattr(bronze_health_monitor.BlobServiceClient, 'from_connection_string', Mock(side_effect=HttpResponseError('secret SAS')))
    with pytest.raises(RuntimeError, match='health check failed'):
        bronze_health_monitor.main(Mock())
    assert '"freshness": "error"' in caplog.text
    assert 'secret SAS' not in caplog.text
