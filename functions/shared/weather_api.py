# Module pour les appels API Weather
import time
import requests
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from .config import (
    WEATHERAPI_FORECAST_ENDPOINT, DEFAULT_FORECAST_DAYS, DEFAULT_LANG,
    API_TIMEOUT_SECONDS, CITIES, CITY_QUERIES, RETRY_MAX_ATTEMPTS,
    RETRY_MIN_WAIT_SECONDS, RETRY_MAX_WAIT_SECONDS, RETRY_AFTER_MAX_SECONDS,
)

logger = logging.getLogger(__name__)


RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


class TransientWeatherAPIError(Exception):
    """Erreur HTTP temporaire, sans URL susceptible de contenir la clé API."""

    def __init__(self, status_code, retry_after=None):
        super().__init__(f"WeatherAPI temporary HTTP status {status_code}")
        self.retry_after = retry_after


def _retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _is_retryable(error):
    if isinstance(error, TransientWeatherAPIError):
        # Un Retry-After trop long bloquerait la Function jusqu'à son timeout.
        return error.retry_after is None or error.retry_after <= RETRY_AFTER_MAX_SECONDS
    return isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))


def _wait_for_retry(retry_state):
    backoff = wait_exponential(
        multiplier=1, min=RETRY_MIN_WAIT_SECONDS, max=RETRY_MAX_WAIT_SECONDS,
    )(retry_state)
    error = retry_state.outcome.exception()
    return max(backoff, getattr(error, "retry_after", None) or 0)


def _log_retry(retry_state):
    # Ne pas journaliser l'exception requests : son URL peut contenir la clé API.
    logger.warning(
        "Retrying WeatherAPI request after %s (attempt %s/%s)",
        type(retry_state.outcome.exception()).__name__,
        retry_state.attempt_number, RETRY_MAX_ATTEMPTS,
    )


# Appel API avec retry limité aux erreurs temporaires.
@retry(
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=_wait_for_retry,
    retry=retry_if_exception(_is_retryable),
    before_sleep=_log_retry,
    reraise=True,
)
def _call_api(city_name, api_key, days, lang):
    response = requests.get(
        WEATHERAPI_FORECAST_ENDPOINT,
        params={"key": api_key, "q": CITY_QUERIES[city_name], "days": days, "lang": lang},
        timeout=API_TIMEOUT_SECONDS
    )
    if response.status_code in RETRYABLE_HTTP_STATUSES:
        error = TransientWeatherAPIError(
            response.status_code,
            _retry_after_seconds(response.headers.get("Retry-After")),
        )
        response.close()
        if not _is_retryable(error):
            logger.warning(
                "WeatherAPI Retry-After of %ss for %s exceeds %ss, not retrying",
                error.retry_after, city_name, RETRY_AFTER_MAX_SECONDS,
            )
        raise error
    return response


# Récupère les données météo pour une ville
def get_weather_data(city_name, api_key, days=DEFAULT_FORECAST_DAYS, lang=DEFAULT_LANG):
    start_time = time.time()

    try:
        response = _call_api(city_name, api_key, days, lang)

        duration_ms = int((time.time() - start_time) * 1000)

        if response.status_code == 200:
            logger.info(f"Weather data retrieved for {city_name} ({duration_ms}ms)")
            return {"city": city_name, "data": response.json(), "status": "success"}

        logger.error(f"API error {response.status_code} for {city_name}")
        return {"city": city_name, "data": None, "status": "error"}

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)

        if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            logger.error(f"Network error for {city_name} after {RETRY_MAX_ATTEMPTS} attempts ({duration_ms}ms)")
        else:
            logger.error(f"Request failed for {city_name}: {type(e).__name__}")

        return {"city": city_name, "data": None, "status": "error"}


# Récupère les données météo pour toutes les villes
def get_all_cities_data(api_key, days=DEFAULT_FORECAST_DAYS, lang=DEFAULT_LANG):
    ingestion_start = datetime.now(timezone.utc)

    ingestion_id = f"{ingestion_start:%Y%m%d_%H%M%S}_{uuid4().hex}"
    ingestion_timestamp = ingestion_start.isoformat()

    logger.info(f"Starting weather ingestion for {len(CITIES)} cities (ID: {ingestion_id})")

    cities_data = []
    for city in CITIES:
        city_data = get_weather_data(city, api_key, days, lang)
        cities_data.append(city_data)

    successful = len([c for c in cities_data if c.get('status') == 'success'])
    failed = len([c for c in cities_data if c.get('status') != 'success'])

    logger.info(f"Ingestion completed: {successful}/{len(CITIES)} cities retrieved")

    if successful == 0:
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "success"

    return {
        "status": status,
        "ingestion_id": ingestion_id,
        "ingestion_timestamp": ingestion_timestamp,
        "forecast_request": {"days": days},
        "cities": cities_data,
        "summary": {
            "total_cities": len(CITIES),
            "successful": successful,
            "failed": failed
        }
    }
