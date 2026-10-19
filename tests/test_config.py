import pytest

from shared import config


def test_valid_configuration():
    config.validate_configuration()


@pytest.mark.parametrize("name", [
    "WEATHERAPI_KEY", "STORAGE_CONNECTION_STRING", "QUEUE_NAME",
    "STAGING_CONTAINER", "BRONZE_CONTAINER",
])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_configuration_names_setting_without_secret(monkeypatch, name, value):
    monkeypatch.setattr(config, name, value)
    with pytest.raises(config.ConfigurationError, match=name) as error:
        config.validate_configuration()
    assert "test-only" not in str(error.value)


@pytest.mark.parametrize("cities", [[], ["Paris", " "], [None]])
def test_invalid_city_list(monkeypatch, cities):
    monkeypatch.setattr(config, "CITIES", cities)
    with pytest.raises(config.ConfigurationError, match="CITIES"):
        config.validate_configuration()


def test_unresolved_key_vault_reference(monkeypatch):
    monkeypatch.setattr(config, "WEATHERAPI_KEY", "@Microsoft.KeyVault(SecretUri=https://test/secrets/weather-api-key)")
    with pytest.raises(config.ConfigurationError, match="unresolved"):
        config.validate_configuration()


@pytest.mark.parametrize("query", [None, "", "London", "51.5", "51.5,-0.1,0", "nan,0", "0,inf", "91,0", "0,-181"])
def test_invalid_coordinate_query_is_rejected(monkeypatch, query):
    monkeypatch.setitem(config.CITY_QUERIES, "Londres", query)
    with pytest.raises(config.ConfigurationError, match="CITY_QUERIES"):
        config.validate_configuration()


def test_new_city_requires_an_explicit_coordinate_query(monkeypatch):
    monkeypatch.setattr(config, "CITIES", ["Paris", "New city"])
    with pytest.raises(config.ConfigurationError, match="CITY_QUERIES"):
        config.validate_configuration()
