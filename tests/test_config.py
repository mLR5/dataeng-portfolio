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
