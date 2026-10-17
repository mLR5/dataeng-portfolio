import azure.functions as func
import logging
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from weather_ingestion_trigger import execute_ingestion

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("=== Manual Weather Ingestion HTTP Trigger Started ===")

    try:
        summary = execute_ingestion()

        if summary['status'] == 'failed':
            log = logger.error
        elif summary['status'] == 'partial':
            log = logger.warning
        else:
            log = logger.info
        log(f"=== Ingestion Completed: {summary['status']} ({summary['duration_ms']}ms) ===")

        return func.HttpResponse(
            body=json.dumps(summary, indent=2, ensure_ascii=False),
            status_code=503 if summary['status'] == 'failed' else 200,
            mimetype="application/json"
        )

    except Exception as e:
        logger.error(f"=== Ingestion Failed ===")
        logger.error(f"Error: {str(e)}", exc_info=True)

        error_response = {
            "status": "failed",
            "error": str(e),
            "error_type": type(e).__name__
        }

        return func.HttpResponse(
            body=json.dumps(error_response, indent=2, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )
