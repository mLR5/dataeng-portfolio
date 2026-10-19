# Configuration centralisée
import math
import os

# Weather API
WEATHERAPI_BASE_URL = "https://api.weatherapi.com/v1"
WEATHERAPI_FORECAST_ENDPOINT = f"{WEATHERAPI_BASE_URL}/forecast.json"
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY")
# Horizon observé et retenu pour l'offre actuelle : trois dates locales.
DEFAULT_FORECAST_DAYS = 3
DEFAULT_LANG = "fr"
API_TIMEOUT_SECONDS = 10

CITIES = ["Paris", "Londres", "Bruxelles"]
# Garder les libellés métier ; ne pas les utiliser comme recherche fournisseur.
CITY_QUERIES = {
    "Paris": "48.8566,2.3522",
    "Londres": "51.5074,-0.1278",
    "Bruxelles": "50.8503,4.3517",
}

# Azure Storage
STORAGE_ACCOUNT_NAME = os.getenv("STORAGE_ACCOUNT_NAME")
STORAGE_CONNECTION_STRING = os.getenv("STORAGE_CONNECTION_STRING")
QUEUE_NAME = os.getenv("QUEUE_NAME", "ingestion-weatherapi")
STAGING_CONTAINER = os.getenv("STAGING_CONTAINER", "staging")
BRONZE_CONTAINER = os.getenv("BRONZE_CONTAINER", "bronze")
DATALAKE_CONTAINER = os.getenv("DATALAKE_CONTAINER", "datalake")
BRONZE_PATH_PREFIX = "weather/weatherapi"
STAGING_PATH_PREFIX = "raw-ingestion/"

# Retry
RETRY_MAX_ATTEMPTS = 3
RETRY_MIN_WAIT_SECONDS = 1
RETRY_MAX_WAIT_SECONDS = 4
# Au-delà, un Retry-After n'est pas attendu : la ville est marquée en échec.
RETRY_AFTER_MAX_SECONDS = 30

# Queue
QUEUE_MESSAGE_TTL_SECONDS = 604800  # 7 jours


class ConfigurationError(ValueError):
    """Configuration invalide, sans exposer les valeurs des secrets."""


def validate_configuration():
    """Valide la configuration avant tout appel API ou stockage."""
    required = {
        "WEATHERAPI_KEY": WEATHERAPI_KEY,
        "STORAGE_CONNECTION_STRING": STORAGE_CONNECTION_STRING,
        "QUEUE_NAME": QUEUE_NAME,
        "STAGING_CONTAINER": STAGING_CONTAINER,
        "BRONZE_CONTAINER": BRONZE_CONTAINER,
    }
    missing = [name for name, value in required.items() if not value or not value.strip()]
    if missing:
        raise ConfigurationError("Missing required settings: " + ", ".join(missing))
    if WEATHERAPI_KEY.startswith("@Microsoft.KeyVault("):
        raise ConfigurationError("WEATHERAPI_KEY Key Vault reference is unresolved")
    if not CITIES or any(not isinstance(city, str) or not city.strip() for city in CITIES):
        raise ConfigurationError("CITIES must contain at least one non-empty city")
    for city in CITIES:
        try:
            latitude, longitude = map(float, CITY_QUERIES[city].split(","))
            if not (math.isfinite(latitude) and -90 <= latitude <= 90
                    and math.isfinite(longitude) and -180 <= longitude <= 180):
                raise ValueError
        except (KeyError, AttributeError, TypeError, ValueError):
            raise ConfigurationError("CITY_QUERIES must provide valid coordinates for every city") from None
