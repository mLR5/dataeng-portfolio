# Configuration centralisée
import os

# Weather API
WEATHERAPI_BASE_URL = "https://api.weatherapi.com/v1"
WEATHERAPI_FORECAST_ENDPOINT = f"{WEATHERAPI_BASE_URL}/forecast.json"
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY")
DEFAULT_FORECAST_DAYS = 7
DEFAULT_LANG = "fr"
API_TIMEOUT_SECONDS = 10

CITIES = ["Paris", "Londres", "Bruxelles"]

# Azure Storage
STORAGE_ACCOUNT_NAME = os.getenv("STORAGE_ACCOUNT_NAME")
STORAGE_CONNECTION_STRING = os.getenv("STORAGE_CONNECTION_STRING")
QUEUE_NAME = os.getenv("QUEUE_NAME", "ingestion-weatherapi")
STAGING_CONTAINER = os.getenv("STAGING_CONTAINER", "staging")
BRONZE_CONTAINER = os.getenv("BRONZE_CONTAINER", "bronze")
DATALAKE_CONTAINER = os.getenv("DATALAKE_CONTAINER", "datalake")
BRONZE_PATH_PREFIX = "weather/weatherapi"

# Retry
RETRY_MAX_ATTEMPTS = 3
RETRY_MIN_WAIT_SECONDS = 1
RETRY_MAX_WAIT_SECONDS = 4

# Queue
QUEUE_MESSAGE_TTL_SECONDS = 604800  # 7 jours
