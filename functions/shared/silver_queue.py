"""Small wake-ups only: Bronze paths and progress remain in the leased checkpoint."""
import json
import os
from datetime import datetime, timezone

from .silver_runtime import _utc, batch_cutoff


def wakeup_body(cutoff):
    cutoff = _utc(cutoff)
    if cutoff != cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
        raise ValueError('Batch cutoff must be UTC midnight')
    return json.dumps({'schema_version': 1, 'cutoff_utc': cutoff.isoformat()})


def read_wakeup(body, settings, *, now=None):
    if not isinstance(body, bytes) or len(body) > 1024:
        raise ValueError('Invalid Silver wake-up size')
    message = json.loads(body)
    if not isinstance(message, dict) or message.get('schema_version') != 1:
        raise ValueError('Invalid Silver wake-up schema')
    cutoff = _utc(message['cutoff_utc'])
    wakeup_body(cutoff)
    if cutoff > batch_cutoff(now or datetime.now(timezone.utc), settings):
        raise ValueError('Silver wake-up includes an unfinished collection day')
    return cutoff


def enqueue_wakeup(settings, cutoff, *, delay=0):
    from azure.storage.queue import QueueClient
    from azure.identity import DefaultAzureCredential
    if type(delay) is not int or not 0 <= delay <= 3600:
        raise ValueError('Invalid Silver continuation delay')
    connection = os.getenv('STORAGE_CONNECTION_STRING') or os.getenv('AzureWebJobsStorage')
    if connection:
        queue = QueueClient.from_connection_string(connection, settings.queue_name)
    else:
        queue = QueueClient(
            f"https://{os.environ['STORAGE_ACCOUNT_NAME']}.queue.core.windows.net",
            settings.queue_name, credential=DefaultAzureCredential(),
        )
    # Terraform (or the isolated validator) owns queue creation. Do not create here.
    with queue:
        queue.send_message(wakeup_body(cutoff), visibility_timeout=delay)
