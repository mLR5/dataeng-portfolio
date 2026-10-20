"""Continue a daily batch; enqueue its successor before acknowledging this message."""
import json
import logging

import azure.functions as func

from shared.silver_queue import enqueue_wakeup, read_wakeup
from shared.silver_runtime import make_runtime, run_tick
from shared.silver_settings import SilverSettings

logger = logging.getLogger(__name__)


def main(message: func.QueueMessage) -> None:
    try:
        settings = SilverSettings.from_env()
        if not settings.enabled:
            logger.info('SILVER_RUN %s', json.dumps({'outcome': 'disabled'}))
            return
        cutoff = read_wakeup(message.get_body(), settings)
        runner, store, _ = make_runtime(settings)
        report = run_tick(runner, store, settings, cutoff=cutoff)
        delay = report.get('next_delay_seconds')
        if delay is not None:
            enqueue_wakeup(settings, report['cutoff_utc'], delay=delay)
    except Exception as error:
        logger.error('SILVER_RUN %s', json.dumps({'outcome': 'error', 'error_type': type(error).__name__}))
        raise RuntimeError('Silver batch failed; durable checkpoint retained') from None
    log = logger.warning if report['outcome'] == 'partial' else logger.info
    log('SILVER_RUN %s', json.dumps(report))
