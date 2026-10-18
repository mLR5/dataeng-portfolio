"""Versioned identity registry: labels are aliases, never primary keys."""
from dataclasses import dataclass
import math
import unicodedata
from typing import Any, Mapping

REGISTRY_VERSION = 1


def _normalise(value: str) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value.strip().casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class CityDefinition:
    city_id: str
    timezone: str
    query_aliases: tuple[str, ...]
    country_aliases: tuple[str, ...]
    latitude_bounds: tuple[float, float]
    longitude_bounds: tuple[float, float]


CITIES = (
    CityDefinition("fr_paris", "Europe/Paris", ("Paris",),
                   ("France",), (48.5, 49.2), (1.9, 2.8)),
    CityDefinition("gb_london", "Europe/London", ("Londres", "London"),
                   ("United Kingdom", "UK", "United Kingdom of Great Britain and Northern Ireland"),
                   (51.1, 51.9), (-0.6, 0.3)),
    CityDefinition("be_brussels", "Europe/Brussels", ("Bruxelles", "Brussels", "Brussel"),
                   ("Belgium", "Belgique", "Belgie"), (50.5, 51.2), (3.9, 4.8)),
)
CITY_BY_ID = {city.city_id: city for city in CITIES}
_CITY_BY_QUERY = {_normalise(alias): city for city in CITIES for alias in city.query_aliases}


class CityResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def city_by_query(query: str) -> CityDefinition | None:
    return _CITY_BY_QUERY.get(_normalise(query))


def resolve_city(query: str, location: Mapping[str, Any]) -> CityDefinition:
    """Validate geography, country and IANA zone independently of source label.

    Bounds intentionally allow provider rounding/nearby metropolitan coordinates.
    Registry changes must be reviewed when adding cities or source aliases.
    """
    city = city_by_query(query)
    if city is None:
        raise CityResolutionError("unknown_city", "Requested city is absent from the versioned registry")
    if not isinstance(location, Mapping):
        raise CityResolutionError("location_mismatch", "Location must be an object")
    if location.get("tz_id") != city.timezone:
        raise CityResolutionError("location_mismatch", "Location timezone differs from the registered city")
    if _normalise(location.get("country")) not in {_normalise(c) for c in city.country_aliases}:
        raise CityResolutionError("location_mismatch", "Location country differs from the registered city")
    for key, bounds in (("lat", city.latitude_bounds), ("lon", city.longitude_bounds)):
        value = location.get(key)
        if isinstance(value, bool):
            raise CityResolutionError("location_mismatch", "Location coordinates are invalid")
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise CityResolutionError("location_mismatch", "Location coordinates are invalid") from None
        if not math.isfinite(number) or not bounds[0] <= number <= bounds[1]:
            raise CityResolutionError("location_mismatch", "Location coordinates differ from the registered city")
    return city
