# Module pour les appels API Weather
import time
import socket
import requests
import logging
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
from .config import WEATHERAPI_FORECAST_ENDPOINT, WEATHERAPI_BASE_URL, DEFAULT_FORECAST_DAYS, DEFAULT_LANG, API_TIMEOUT_SECONDS, CITIES

logger = logging.getLogger(__name__)


# Appel API avec retry
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError)),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def _call_api(city_name, api_key, days, lang):
    return requests.get(
        WEATHERAPI_FORECAST_ENDPOINT,
        params={"key": api_key, "q": city_name, "days": days, "lang": lang},
        timeout=API_TIMEOUT_SECONDS
    )


# Récupère les données météo pour une ville
def get_weather_data(city_name, api_key, days=DEFAULT_FORECAST_DAYS, lang=DEFAULT_LANG):
    start_time = time.time()
    timestamp = datetime.utcnow().isoformat() + "Z"

    metadata = {
        "city": city_name,
        "timestamp": timestamp,
        "endpoint": WEATHERAPI_FORECAST_ENDPOINT,
        "forecast_days": days,
        "language": lang
    }

    try:
        response = _call_api(city_name, api_key, days, lang)

        duration_ms = int((time.time() - start_time) * 1000)
        metadata["duration_ms"] = duration_ms
        metadata["http_status"] = response.status_code

        if response.status_code == 200:
            logger.info(f"Weather data retrieved for {city_name} ({duration_ms}ms)")
            return {"metadata": metadata, "raw_data": response.json()}

        logger.error(f"API error {response.status_code} for {city_name}")
        metadata["error"] = f"http_{response.status_code}"
        metadata["error_message"] = response.text[:100] if response.text else "Unknown error"
        return {"metadata": metadata, "raw_data": None}

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)

        if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            logger.error(f"Network error for {city_name} after 3 retries ({duration_ms}ms): {str(e)}")
        else:
            logger.error(f"Request failed for {city_name}: {str(e)}")

        metadata["duration_ms"] = duration_ms
        metadata["error"] = type(e).__name__
        metadata["error_message"] = str(e)
        return {"metadata": metadata, "raw_data": None}


# Récupère les données météo pour toutes les villes
def get_all_cities_data(api_key, days=DEFAULT_FORECAST_DAYS, lang=DEFAULT_LANG):
    ingestion_start = datetime.utcnow()

    ingestion_metadata = {
        "ingestion_id": ingestion_start.strftime('%Y%m%d_%H%M%S'),
        "ingestion_timestamp": ingestion_start.isoformat() + "Z",
        "source_api": "WeatherAPI",
        "source_url": WEATHERAPI_BASE_URL,
        "pipeline": "bronze_weather_ingestion",
        "environment": "dev",
        "host": socket.gethostname(),
        "cities_requested": CITIES,
        "forecast_days": days
    }

    logger.info(f"Starting weather ingestion for {len(CITIES)} cities (ID: {ingestion_metadata['ingestion_id']})")

    cities_data = []
    for city in CITIES:
        city_data = get_weather_data(city, api_key, days, lang)
        cities_data.append(city_data)

    summary = {
        "total_cities_requested": len(CITIES),
        "total_cities_retrieved": len([c for c in cities_data if c.get('raw_data')]),
        "total_cities_failed": len([c for c in cities_data if not c.get('raw_data')])
    }

    logger.info(f"Ingestion completed: {summary['total_cities_retrieved']}/{summary['total_cities_requested']} cities retrieved")

    return {
        "ingestion_metadata": ingestion_metadata,
        "cities": cities_data,
        "summary": summary
    }
