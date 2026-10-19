"""Automatic bounded Silver batch; failures remain durable for the next tick."""
import json
import logging

import azure.functions as func

from shared.silver_runtime import make_runtime, run_tick
from shared.silver_settings import SilverSettings

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    try:
        settings = SilverSettings.from_env()
        if not settings.enabled:
            logger.info('SILVER_RUN %s', json.dumps({'outcome': 'disabled'}))
            return
        runner, store, _ = make_runtime(settings)
        report = run_tick(runner, store, settings)
    except Exception as error:
        logger.error('SILVER_RUN %s', json.dumps({'outcome': 'error', 'error_type': type(error).__name__}))
        raise RuntimeError('Silver batch failed; see SILVER_RUN event') from None
    log = logger.warning if report['outcome'] == 'partial' else logger.info
    log('SILVER_RUN %s', json.dumps(report))
    if report['outcome'] == 'partial':
        raise RuntimeError('Silver has retryable failures; see SILVER_RUN event')
