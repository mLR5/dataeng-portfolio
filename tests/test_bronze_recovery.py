import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from shared import claim_check
from shared.bronze_recovery import RecoveryService

INGESTION_ID = '20261017_120000_' + 'a' * 32
TIMESTAMP = '2026-10-17T12:00:00+00:00'


@pytest.fixture
def recovery():
    data = {
        'ingestion_id': INGESTION_ID, 'ingestion_timestamp': TIMESTAMP,
        'status': 'success', 'summary': {'total_cities': 3, 'successful': 3, 'failed': 0},
        'cities': [{'city': 'Paris', 'data': {'condition': 'éclaircies'}}],
    }
    staging = Mock(url='https://test.blob.core.windows.net/staging/raw-ingestion/test.json')
    staging.download_blob.return_value.readall.side_effect = lambda: json.dumps(data, ensure_ascii=False).encode()
    bronze = Mock()
    bronze.exists.return_value = False
    blobs = Mock()
    blobs.get_blob_client.side_effect = lambda container, name: bronze if container == 'bronze' else staging
    queue = Mock()
    queue.send_message.return_value.id = 'published-message'
    poison = Mock()
    message = claim_check.build_message(data, {
        'container': 'staging', 'blob_name': claim_check.staging_blob_name(INGESTION_ID),
        'url': staging.url, 'size_bytes': 123,
    })
    item = SimpleNamespace(id='poison-id', pop_receipt='secret-pop-receipt', content=json.dumps(message))
    poison.peek_messages.return_value = [item]
    poison.receive_messages.return_value = [item]
    return SimpleNamespace(
        service=RecoveryService(blobs, queue, poison), data=data, staging=staging,
        bronze=bronze, blobs=blobs, queue=queue, poison=poison, item=item, message=message,
    )


def test_staging_preview_never_mutates_azure(recovery):
    result = recovery.service.recover_staging(INGESTION_ID)
    assert result.outcome == 'preview'
    recovery.queue.send_message.assert_not_called()
    recovery.staging.delete_blob.assert_not_called()


def test_staging_execute_publishes_canonical_message_with_exact_utf8_size(recovery):
    result = recovery.service.recover_staging(INGESTION_ID, execute=True)
    assert result.outcome == 'published'
    body = claim_check.parse_message(recovery.queue.send_message.call_args.args[0])
    assert body['payload_size_bytes'] == len(json.dumps(recovery.data, ensure_ascii=False).encode())
    assert body['ingestion_id'] == INGESTION_ID
    assert recovery.queue.send_message.call_args.kwargs['time_to_live'] == 604800
    recovery.staging.delete_blob.assert_not_called()


def test_staging_already_bronze_does_not_publish(recovery):
    recovery.bronze.exists.return_value = True
    assert recovery.service.recover_staging(INGESTION_ID, execute=True).outcome == 'already_bronze'
    recovery.queue.send_message.assert_not_called()


def test_staging_expired_is_explicitly_blocked(recovery):
    recovery.staging.download_blob.side_effect = ResourceNotFoundError('expired')
    result = recovery.service.recover_staging(INGESTION_ID, execute=True)
    assert (result.outcome, result.reason) == ('blocked', 'staging_missing_or_expired')
    recovery.queue.send_message.assert_not_called()


@pytest.mark.parametrize('ingestion_id', ['../../secret', 'not-an-id', None])
def test_bad_ids_are_rejected_before_access(recovery, ingestion_id):
    assert recovery.service.recover_staging(ingestion_id, execute=True).outcome == 'blocked'
    recovery.blobs.get_blob_client.assert_not_called()


def test_staging_identity_mismatch_is_blocked(recovery):
    recovery.data['ingestion_id'] = '20261017_120000'
    assert recovery.service.recover_staging(INGESTION_ID, execute=True).outcome == 'blocked'
    recovery.queue.send_message.assert_not_called()


def test_old_staging_without_status_remains_recoverable(recovery):
    del recovery.data['status']
    assert recovery.service.recover_staging(INGESTION_ID, execute=True).outcome == 'published'
    assert json.loads(recovery.queue.send_message.call_args.args[0])['status'] == 'success'


def test_recovery_does_not_publish_sas_in_blob_url(recovery):
    recovery.staging.url += '?sv=version&sig=secret'
    assert recovery.service.recover_staging(INGESTION_ID, execute=True).outcome == 'published'
    assert '?' not in json.loads(recovery.queue.send_message.call_args.args[0])['blob_url']


def test_failed_collection_can_still_be_archived_without_recollecting(recovery):
    recovery.data.update(status='failed', summary={'total_cities': 3, 'successful': 0, 'failed': 3})
    assert recovery.service.recover_staging(INGESTION_ID, execute=True).outcome == 'published'
    assert json.loads(recovery.queue.send_message.call_args.args[0])['status'] == 'failed'


@pytest.mark.parametrize('summary', [None, {}, {'total_cities': 3, 'successful': 1, 'failed': 0}])
def test_invalid_staging_summary_is_not_published(recovery, summary):
    recovery.data['summary'] = summary
    assert recovery.service.recover_staging(INGESTION_ID, execute=True).outcome == 'blocked'
    recovery.queue.send_message.assert_not_called()


def test_poison_preview_uses_peek_and_does_not_ack(recovery):
    result, = recovery.service.recover_poison()
    assert result.outcome == 'preview'
    recovery.poison.peek_messages.assert_called_once_with(max_messages=1)
    recovery.poison.receive_messages.assert_not_called()
    recovery.poison.delete_message.assert_not_called()
    recovery.queue.send_message.assert_not_called()
    assert 'secret-pop-receipt' not in json.dumps(result.as_dict())


def test_absent_poison_queue_is_reported_without_creation(recovery):
    recovery.poison.peek_messages.side_effect = ResourceNotFoundError('no queue yet')
    result, = recovery.service.recover_poison()
    assert result.outcome == 'poison_queue_absent'
    recovery.poison.create_queue.assert_not_called()
    recovery.poison.delete_message.assert_not_called()


def test_absent_poison_queue_is_handled_on_lazy_receive(recovery):
    def missing_messages():
        raise ResourceNotFoundError('no queue yet')
        yield
    recovery.poison.receive_messages.return_value = missing_messages()
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'poison_queue_absent'
    recovery.poison.delete_message.assert_not_called()


def test_poison_listing_access_error_is_not_hidden_as_empty_queue(recovery):
    recovery.poison.peek_messages.side_effect = HttpResponseError('access denied')
    with pytest.raises(HttpResponseError):
        recovery.service.recover_poison()


def test_poison_publish_is_confirmed_before_ack(recovery):
    operations = Mock()
    operations.attach_mock(recovery.queue.send_message, 'publish')
    operations.attach_mock(recovery.poison.delete_message, 'delete')
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'published_acknowledged'
    assert [operation[0] for operation in operations.mock_calls] == ['publish', 'delete']
    recovery.poison.delete_message.assert_called_once_with('poison-id', 'secret-pop-receipt')
    recovery.poison.receive_messages.assert_called_once_with(
        messages_per_page=1, max_messages=1, visibility_timeout=120,
    )


@pytest.mark.parametrize('error', [HttpResponseError('SAS=secret'), RuntimeError('SAS=secret')])
def test_poison_publish_error_never_deletes_original_or_leaks_sdk_detail(recovery, error):
    recovery.queue.send_message.side_effect = error
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'error'
    assert 'secret' not in json.dumps(result.as_dict())
    recovery.poison.delete_message.assert_not_called()


def test_unconfirmed_publish_never_deletes_poison(recovery):
    recovery.queue.send_message.return_value.id = None
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'error'
    recovery.poison.delete_message.assert_not_called()


def test_poison_ack_failure_reports_duplicate_risk(recovery):
    recovery.poison.delete_message.side_effect = HttpResponseError('SAS=secret')
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'published_unacknowledged'
    assert 'secret' not in json.dumps(result.as_dict())


def test_poison_missing_staging_and_bronze_is_left_for_investigation(recovery):
    recovery.staging.download_blob.side_effect = ResourceNotFoundError('missing')
    result, = recovery.service.recover_poison(execute=True)
    assert (result.outcome, result.reason) == ('blocked', 'staging_missing_or_expired')
    recovery.queue.send_message.assert_not_called()
    recovery.poison.delete_message.assert_not_called()


def test_poison_existing_bronze_can_be_acknowledged_without_source(recovery):
    recovery.bronze.exists.return_value = True
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'already_bronze_acknowledged'
    recovery.queue.send_message.assert_not_called()
    recovery.staging.download_blob.assert_not_called()
    recovery.poison.delete_message.assert_called_once()


def test_poison_concurrent_promotion_is_recognized(recovery):
    recovery.bronze.exists.side_effect = [False, True]
    recovery.staging.download_blob.side_effect = ResourceNotFoundError('removed concurrently')
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'already_bronze_acknowledged'
    recovery.queue.send_message.assert_not_called()


def test_poison_timestamp_mismatch_cannot_publish_or_ack(recovery):
    recovery.message['ingestion_timestamp'] = '2026-10-17T13:00:00+00:00'
    recovery.item.content = json.dumps(recovery.message)
    result, = recovery.service.recover_poison(execute=True)
    assert (result.outcome, result.reason) == ('blocked', 'staging_timestamp_mismatch')
    recovery.queue.send_message.assert_not_called()
    recovery.poison.delete_message.assert_not_called()


def test_poison_equivalent_timestamp_in_other_timezone_is_valid(recovery):
    recovery.message['ingestion_timestamp'] = '2026-10-17T14:00:00+02:00'
    recovery.item.content = json.dumps(recovery.message)
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'published_acknowledged'


def test_invalid_poison_is_preserved_without_blob_access(recovery):
    recovery.item.content = '{"secret": "not a claim check"}'
    result, = recovery.service.recover_poison(execute=True)
    assert result.outcome == 'blocked'
    recovery.blobs.get_blob_client.assert_not_called()
    recovery.poison.delete_message.assert_not_called()


def test_staging_scan_only_reports_old_candidates_and_never_publishes(recovery):
    now = datetime(2026, 10, 17, 13, tzinfo=timezone.utc)
    container = recovery.blobs.get_container_client.return_value
    container.list_blobs.return_value = [
        SimpleNamespace(name=claim_check.staging_blob_name(INGESTION_ID), last_modified=now),
        SimpleNamespace(name=claim_check.staging_blob_name(INGESTION_ID), last_modified=now.replace(hour=12)),
    ]
    result, = recovery.service.scan_staging(now=now)
    assert result.outcome == 'preview'
    recovery.queue.send_message.assert_not_called()


def test_staging_scan_reports_truncation_instead_of_scanning_unbounded_history(recovery):
    now = datetime(2026, 10, 17, 13, tzinfo=timezone.utc)
    recovery.blobs.get_container_client.return_value.list_blobs.return_value = (
        SimpleNamespace(name=claim_check.staging_blob_name(INGESTION_ID), last_modified=now)
        for _ in range(2000)
    )
    result, = recovery.service.scan_staging(now=now)
    assert result.outcome == 'scan_truncated'
    recovery.staging.download_blob.assert_not_called()


def load_cli():
    path = Path(__file__).parents[1] / 'scripts' / 'bronze_recovery.py'
    spec = importlib.util.spec_from_file_location('recovery_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_is_preview_by_default(recovery, monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setattr(cli, 'make_service', lambda: recovery.service)
    assert cli.main(['staging', '--ingestion-id', INGESTION_ID]) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'preview'
    recovery.queue.send_message.assert_not_called()


def test_cli_returns_failure_without_exposing_exception_message(monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setattr(cli, 'make_service', Mock(side_effect=RuntimeError('secret connection string')))
    assert cli.main(['poison']) == 1
    assert json.loads(capsys.readouterr().err) == {'outcome': 'error', 'reason': 'RuntimeError'}


@pytest.mark.parametrize('limit', ['0', '33', '-1'])
def test_cli_rejects_unbounded_poison_batches(limit):
    with pytest.raises(SystemExit) as error:
        load_cli().main(['poison', '--limit', limit])
    assert error.value.code == 2
