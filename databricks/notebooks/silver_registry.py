# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : registre des chemins Bronze
# MAGIC
# MAGIC Chaque chemin JSON observé en Bronze porte une décision écrite. Les chemins
# MAGIC inconnus sont listés dans `summary["unknown_paths"]` de la publication et
# MAGIC déclenchent une alerte ; ils ne font jamais évoluer un schéma.
# MAGIC
# MAGIC | Décision | Sens |
# MAGIC |---|---|
# MAGIC | `kept` | stocké tel quel ou normalisé (colonne Silver déjà présente en v1) |
# MAGIC | `added` | nouveau en v2 (colonne Silver indiquée) |
# MAGIC | `derivable` | non stocké : se recalcule depuis un champ stocké (indiqué) |
# MAGIC | `represented` | stocké sous une autre forme (ex. epoch -> instant UTC) |
# MAGIC | `control` | conservé dans la publication / les contrôles, pas dans les lignes météo |
# MAGIC | `excluded` | volontairement non stocké (raison indiquée) |
# MAGIC | `structural` | enveloppe / identité, utilisé par les contrôles |
# MAGIC
# MAGIC Les patterns couvrent les feuilles SCALAIRES de l'enveloppe au format courant
# MAGIC (après conversion de l'ancien format) ; un élément de liste s'écrit `[]`, ex.
# MAGIC `cities[].data.forecast.forecastday[].hour[].wetbulb_c`. Les conteneurs sont
# MAGIC implicites. `unknown_paths` signale :
# MAGIC - toute feuille scalaire (null compris) dont le pattern n'a pas de décision ;
# MAGIC - un dict ou une liste VIDE sous un chemin inconnu, par le chemin du conteneur
# MAGIC   lui-même (ex. `cities[].data.current.new_block`) : un bloc nouveau vide reste visible ;
# MAGIC - les nouvelles feuilles sous un scalaire connu devenu conteneur (`...uv.value`).
# MAGIC
# MAGIC Un chemin connu avec un type inattendu (`condition` null, liste `hour` vide, dict à
# MAGIC la place d'un scalaire) n'est pas « inconnu » : les contrôles qualité gèrent les types.

# COMMAND ----------

REGISTRY_DECISIONS = ("kept", "added", "derivable", "represented", "control", "excluded", "structural")
# Decisions that name a Silver destination; the others give a reason.
_REGISTRY_DESTINATION_DECISIONS = frozenset({"kept", "added", "represented", "control", "structural"})


def _registry_decisions():
    """Build FIELD_DECISIONS: pattern -> {"decision", "destination" | "reason"}.

    Destinations use "table.column"; "*.column" means every Silver table.
    """
    city = "cities[]."
    location = "cities[].data.location."
    current = "cities[].data.current."
    forecastday = "cities[].data.forecast.forecastday[]."
    day = forecastday + "day."
    astro = forecastday + "astro."
    hour = forecastday + "hour[]."
    obs, hourly, daily = OBSERVATION_TABLE, FORECAST_TABLE, FORECAST_DAILY_TABLE
    decisions = {}

    def put(pattern, decision, text):
        if pattern in decisions:
            raise ValueError(f"Duplicate registry pattern {pattern}")
        key = "destination" if decision in _REGISTRY_DESTINATION_DECISIONS else "reason"
        decisions[pattern] = {"decision": decision, key: text}

    # Envelope and identity, read by the envelope checks.
    put("ingestion_id", "structural", "identity check (format, blob path); stored as *.ingestion_id")
    put("ingestion_timestamp", "structural",
        "UTC partition and blob path check; stored as *.collected_at_utc, *.ingestion_date")
    put("status", "structural", "must agree with the city statuses (absent from old archives)")
    put("summary.total_cities", "structural", "summary_mismatch check against cities[]")
    put("summary.successful", "structural", "summary_mismatch check against cities[]")
    put("summary.failed", "structural", "summary_mismatch check against cities[]")
    # Validation report written by an earlier collector version (32 archives, 2026):
    # Silver re-validates every value itself and never trusts this verdict.
    data_quality_reason = "collector-side validation report (older collector); Silver re-validates independently"
    for name in ("validation_timestamp", "is_valid", "checks_performed[]", "error_count", "errors[]"):
        put("_data_quality." + name, "excluded", data_quality_reason)
    put("forecast_request.days", "control", "publication summary requested_horizon_days (horizon coverage)")
    put(city + "status", "structural", "collection status: error -> collection_failed rejection")
    put(city + "city", "kept", "*.city_query (resolved to *.city_id by the city registry)")

    # Location: identity and geography checks, stored on every row.
    put(location + "name", "kept", "*.source_location_name")
    put(location + "country", "kept", "*.source_country (country alias check)")
    put(location + "lat", "kept", "*.latitude (bounds check)")
    put(location + "lon", "kept", "*.longitude (bounds check)")
    put(location + "tz_id", "kept", "*.source_timezone (must equal the registered IANA zone)")
    put(location + "region", "control", "publication controls (provider label, not a weather measure)")
    put(location + "localtime", "control", "publication controls (provider local clock at response time)")
    put(location + "localtime_epoch", "represented",
        "response instant; Silver carries the collection instant as *.collected_at_utc")

    # Weather measures shared by current (observations) and hour[] (hourly forecasts).
    for prefix, table in ((current, obs), (hour, hourly)):
        put(prefix + "temp_c", "kept", f"{table}.temperature_c")
        put(prefix + "feelslike_c", "kept", f"{table}.feels_like_c")
        put(prefix + "humidity", "kept", f"{table}.humidity_pct")
        put(prefix + "pressure_mb", "kept", f"{table}.pressure_mb")
        put(prefix + "wind_kph", "kept", f"{table}.wind_kph")
        put(prefix + "wind_degree", "kept", f"{table}.wind_degree")
        put(prefix + "precip_mm", "kept", f"{table}.precipitation_mm")
        put(prefix + "cloud", "kept", f"{table}.cloud_pct")
        put(prefix + "condition.code", "kept", f"{table}.condition_code")
        put(prefix + "condition.text", "kept", f"{table}.condition_text")
        put(prefix + "is_day", "kept", f"{table}.is_day")
        put(prefix + "uv", "added", f"{table}.uv_index")
        put(prefix + "gust_kph", "added", f"{table}.wind_gust_kph")
        put(prefix + "vis_km", "added", f"{table}.visibility_km")
        put(prefix + "condition.icon", "derivable", f"icon URL of ({prefix}condition.code, {prefix}is_day)")
        put(prefix + "wind_dir", "derivable", f"16-point compass label of {prefix}wind_degree")

    put(current + "last_updated_epoch", "represented", f"{obs}.observed_at_utc (checked against last_updated)")
    put(current + "last_updated", "represented", f"{obs}.observed_at_utc (local time in source_timezone)")
    for name in ("chance_of_rain", "chance_of_snow", "will_it_rain", "will_it_snow"):
        put(current + name, "excluded",
            "present in part of the archives only; meaning for a current observation is undocumented")

    put(hour + "time_epoch", "represented", f"{hourly}.target_at_utc (checked against time)")
    put(hour + "time", "represented", f"{hourly}.target_at_utc, {hourly}.target_local_date")
    put(hour + "chance_of_rain", "kept", f"{hourly}.chance_of_rain_pct (DOUBLE in [0, 100])")
    put(hour + "chance_of_snow", "kept", f"{hourly}.chance_of_snow_pct (DOUBLE in [0, 100])")
    put(hour + "dewpoint_c", "added", f"{hourly}.dew_point_c")
    put(hour + "snow_cm", "added", f"{hourly}.snowfall_cm")
    put(hour + "heatindex_c", "added", f"{hourly}.heat_index_c")
    put(hour + "windchill_c", "added", f"{hourly}.wind_chill_c")
    put(hour + "wetbulb_c", "added", f"{hourly}.wet_bulb_c")
    for name, source in (("will_it_rain", "chance_of_rain"), ("will_it_snow", "chance_of_snow")):
        put(hour + name, "excluded", f"0/1 flag derived by the provider from {hour}{source}")

    # Forecast day: official daily forecast and sun events (v2 forecast_daily_snapshots).
    put(forecastday + "date", "added", f"{daily}.target_local_date, {daily}.lead_days (also checks hour[] dates)")
    put(forecastday + "date_epoch", "represented", f"{daily}.target_local_date (local calendar date)")
    put(day + "mintemp_c", "added", f"{daily}.temperature_min_c")
    put(day + "maxtemp_c", "added", f"{daily}.temperature_max_c")
    put(day + "avgtemp_c", "added", f"{daily}.temperature_avg_c")
    put(day + "maxwind_kph", "added", f"{daily}.wind_max_kph")
    put(day + "totalprecip_mm", "added", f"{daily}.precipitation_total_mm")
    put(day + "totalsnow_cm", "added", f"{daily}.snowfall_total_cm")
    put(day + "avgvis_km", "added", f"{daily}.visibility_avg_km")
    put(day + "avghumidity", "added", f"{daily}.humidity_avg_pct")
    put(day + "daily_chance_of_rain", "added", f"{daily}.chance_of_rain_pct")
    put(day + "daily_chance_of_snow", "added", f"{daily}.chance_of_snow_pct")
    put(day + "daily_will_it_rain", "added", f"{daily}.will_it_rain")
    put(day + "daily_will_it_snow", "added", f"{daily}.will_it_snow")
    put(day + "uv", "added", f"{daily}.uv_index")
    put(day + "avgwetbulb_c", "added", f"{daily}.wet_bulb_avg_c (absent from older archives)")
    put(day + "maxwetbulb_c", "added", f"{daily}.wet_bulb_max_c (absent from older archives)")
    put(day + "condition.code", "added", f"{daily}.condition_code")
    put(day + "condition.text", "added", f"{daily}.condition_text")
    put(day + "condition.icon", "derivable", f"icon URL of {day}condition.code (day variant)")
    put(astro + "sunrise", "added", f"{daily}.sunrise_local, {daily}.sunrise_utc")
    put(astro + "sunset", "added", f"{daily}.sunset_local, {daily}.sunset_utc")
    for name in ("moonrise", "moonset", "moon_phase", "moon_illumination", "is_moon_up", "is_sun_up"):
        put(astro + name, "excluded", "no use case for moon data or the instantaneous sun state")

    # Imperial units: conversions of a stored metric field.
    imperial = (
        (current, ("temp_f", "temp_c"), ("feelslike_f", "feelslike_c"), ("wind_mph", "wind_kph"),
         ("gust_mph", "gust_kph"), ("pressure_in", "pressure_mb"), ("precip_in", "precip_mm"),
         ("vis_miles", "vis_km")),
        (hour, ("temp_f", "temp_c"), ("feelslike_f", "feelslike_c"), ("wind_mph", "wind_kph"),
         ("gust_mph", "gust_kph"), ("pressure_in", "pressure_mb"), ("precip_in", "precip_mm"),
         ("vis_miles", "vis_km"), ("dewpoint_f", "dewpoint_c"), ("heatindex_f", "heatindex_c"),
         ("windchill_f", "windchill_c"), ("wetbulb_f", "wetbulb_c")),
        (day, ("mintemp_f", "mintemp_c"), ("maxtemp_f", "maxtemp_c"), ("avgtemp_f", "avgtemp_c"),
         ("maxwind_mph", "maxwind_kph"), ("totalprecip_in", "totalprecip_mm"),
         ("avgvis_miles", "avgvis_km"), ("avgwetbulb_f", "avgwetbulb_c"), ("maxwetbulb_f", "maxwetbulb_c")),
    )
    for prefix, *pairs in imperial:
        for name, metric in pairs:
            put(prefix + name, "derivable", f"imperial unit of {prefix}{metric}")
    return decisions


def _registry_tokens(pattern):
    """'a[].b' -> ['a', None, 'b']; None stands for "any list item"."""
    tokens = []
    for part in pattern.split("."):
        name, depth = part, 0
        while name.endswith("[]"):
            name, depth = name[:-2], depth + 1
        if not name or "[" in name or "]" in name:
            raise ValueError(f"Invalid registry pattern {pattern}")
        tokens.append(name)
        tokens.extend([None] * depth)
    return tokens


def _registry_trie(decisions):
    """Nested nodes [children by key, list-item node, pattern] for a single cheap walk."""
    root = [{}, None, ""]
    for pattern, entry in decisions.items():
        if entry.get("decision") not in REGISTRY_DECISIONS or not (entry.get("destination") or entry.get("reason")):
            raise ValueError(f"Registry entry for {pattern} needs a known decision and a destination or reason")
        node, path = root, ""
        for token in _registry_tokens(pattern):
            if token is None:
                path += "[]"
                if node[1] is None:
                    node[1] = [{}, None, path]
                node = node[1]
            else:
                path = f"{path}.{token}" if path else token
                node = node[0].setdefault(token, [{}, None, path])
    return root


# Path pattern -> decision. Lists are written with [], e.g. "cities[].data.current.uv".
FIELD_DECISIONS = _registry_decisions()
_REGISTRY_TRIE = _registry_trie(FIELD_DECISIONS)


def unknown_paths(data):
    """Sorted scalar paths of ``data`` (a current-shape envelope) without a decision.

    One iterative walk guided by the registry trie: a known key costs one dict
    lookup and path strings are only built under unknown keys. Never raises on
    JSON-like input; a non-dict envelope gives [].
    """
    if not isinstance(data, dict):
        return []
    found = set()
    seen = set()  # JSON is acyclic; this only guards odd Python input against loops.
    stack = [(data, _REGISTRY_TRIE, "")]
    while stack:
        value, node, path = stack.pop()
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                continue
            seen.add(id(value))
            if node is None and not value:
                found.add(path)  # new empty container: reported as itself
            elif isinstance(value, dict):
                children = node[0] if node is not None else {}
                for key, child in value.items():
                    child_node = children.get(key) if isinstance(key, str) else None
                    if child_node is not None:
                        stack.append((child, child_node, child_node[2]))
                    else:
                        name = key if isinstance(key, str) else repr(key)
                        stack.append((child, None, f"{path}.{name}" if path else name))
            else:
                item_node = node[1] if node is not None else None
                item_path = item_node[2] if item_node is not None else path + "[]"
                stack.extend((item, item_node, item_path) for item in value)
        elif node is None:
            found.add(path)
    return sorted(found)


def decisions_table():
    """Documentation rows (pattern, decision, destination_or_reason), sorted by pattern."""
    return [(pattern, entry["decision"], entry.get("destination") or entry.get("reason"))
            for pattern, entry in sorted(FIELD_DECISIONS.items())]
