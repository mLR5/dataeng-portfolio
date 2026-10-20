"""Independent Silver health timer, including a stopped or failing worker."""
import json
import logging

import azure.functions as func

from shared.silver_health import check_silver_health
from shared.silver_publication import json_bytes
from shared.silver_runtime import make_runtime
from shared.silver_settings import SilverSettings

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    try:
        settings = SilverSettings.from_env()
        if settings.enabled:
            runner, store, _ = make_runtime(settings)
        else:
            store = None
        health = check_silver_health(store, settings)
        if settings.enabled:
            # Diagnostic snapshot only. Never alter coordinator, source or tables.
            runner.controls.blob('runtime/health.json').upload_blob(json_bytes(health), overwrite=True)
    except Exception as error:
        logger.error('SILVER_HEALTH %s', json.dumps({
            'schema_version': 1, 'freshness': 'error', 'error_type': type(error).__name__,
        }))
        raise RuntimeError('Silver health check failed; see SILVER_HEALTH event') from None
    logger.info('SILVER_HEALTH %s', json.dumps(health))
