"""Open one daily J+1 batch; its queue continues bounded work to completion."""
import json
import logging
from datetime import datetime, timezone

import azure.functions as func

from shared.silver_queue import enqueue_wakeup
from shared.silver_runtime import batch_cutoff
from shared.silver_settings import SilverSettings

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    try:
        settings = SilverSettings.from_env()
        if not settings.enabled:
            logger.info('SILVER_RUN %s', json.dumps({'outcome': 'disabled'}))
            return
        cutoff = batch_cutoff(datetime.now(timezone.utc), settings)
        enqueue_wakeup(settings, cutoff)
        logger.info('SILVER_RUN %s', json.dumps({'outcome': 'scheduled', 'cutoff_utc': cutoff.isoformat()}))
    except Exception as error:
        logger.error('SILVER_RUN %s', json.dumps({'outcome': 'error', 'error_type': type(error).__name__}))
        raise RuntimeError('Silver daily scheduling failed; see SILVER_RUN event') from None
