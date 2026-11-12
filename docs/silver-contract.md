# Silver WeatherAPI v1 — contrats et preuve de faisabilité Delta

Contrat stabilisé pour les lots S1/S2/S3. Source : JSON Bronze déjà archivé ;
aucun appel WeatherAPI par Silver. Deux tables Delta, sans table quotidienne
supplémentaire. Les exemples synthétiques servent aux tests ; seules les fixtures
accompagnées d'une provenance réelle constituent des observations du fournisseur.

## Versions et interface communes

- Le notebook `databricks/notebooks/silver_rules.py` est l'unique définition des
  schémas, clés, versions et dataclasses (section « Contrat Silver »). Il est
  chargé par `%run` dans le pipeline Spark et testé en CI ; la Function App ne
  contient plus aucun code Silver.
- `SCHEMA_VERSION=1`, `TRANSFORM_VERSION="1.0.0"`, `SOURCE="weatherapi"`.
- Exécution : Databricks serverless. Tests : Python 3.11 Linux en CI avec
  **PyArrow 21.0.0**, PySpark et Delta Lake (`requirements-databricks.txt`).
- `transform_bronze(raw: bytes, source_blob: str) -> TransformResult` est défini
  dans le même notebook, sans I/O.
- Le pipeline (`silver_pipeline.py`) consomme `result.tables`, `TABLE_SCHEMAS`,
  `TABLE_KEYS`, `result.rejections` et `result.summary`.

`TransformResult` contient les tables Arrow `observations`, `forecasts`, les
alias `observation_snapshots`/`forecast_hourly_snapshots`, les rejets immuables,
le bilan JSON, le statut, `ingestion_id`, `collected_at_utc`, `source_blob` et
`source_sha256`. L'identité et la date peuvent être nulles si le JSON ne permet
pas de les vérifier ; la provenance reste disponible sans ligne métier. Le
runner S3 retrouve l'identité du fichier Bronze canonique pour journaliser le
rejet. Une exception technique reste réessayable ; elle n'est pas convertie en
rejet de qualité.

## Deux tables et leurs grains

| Table | Grain | Clé unique |
| --- | --- | --- |
| `observation_snapshots` | Observation d'une ville lors d'une collecte | `ingestion_id, city_id, observed_at_utc` |
| `forecast_hourly_snapshots` | Prévision d'une ville pour une heure cible lors d'une collecte | `ingestion_id, city_id, target_at_utc` |

Deux collectes d'une même heure cible restent deux snapshots. Deux observations
identiques dans des collectes distinctes restent également deux snapshots ; Gold
pourra produire une série canonique. `collected_at_utc` est le début de collecte,
pas une heure d'émission officielle de prévision. Les cibles déjà passées restent
des données de prévision et portent `target_is_past_at_collection=true`.

## Types et nullabilité

Toutes les colonnes sont déclarées explicitement. Les timestamps sont
`timestamp(us, UTC)` ; les dates sont `date32`. Aucun schéma n'est inféré depuis
le premier lot. Une table sans ligne conserve le même schéma.

| Colonnes communes | Type Arrow | Null autorisé |
| --- | --- | --- |
| `schema_version` | int16 | non |
| `transform_version`, `source`, `ingestion_id` | string | non |
| `collected_at_utc` | timestamp(us, UTC) | non |
| `ingestion_date` | date32, jour UTC de collecte | non |
| `city_id`, `city_query` | string | non |
| `source_location_name`, `source_country` | string | oui |
| `latitude`, `longitude` | float64 | non |
| `source_timezone`, `source_blob`, `source_sha256` | string | non |

Les coordonnées et le pays sont validés contre le registre avant production.
`source_country` peut être nullable dans le schéma pour l'évolutivité des lecteurs,
mais une source sans pays identifiable ne satisfait pas le registre v1.
`source_blob` est le chemin relatif sans SAS, et `source_sha256` porte sur les
**octets Bronze exacts**, avant parsing JSON.

| Colonnes météo des deux tables | Type | Null autorisé |
| --- | --- | --- |
| `temperature_c` | float64 | non |
| `feels_like_c`, `pressure_mb`, `wind_kph`, `precipitation_mm` | float64 | oui |
| `humidity_pct`, `wind_degree`, `cloud_pct` | int16 | oui |
| `condition_code` | int32 | oui |
| `condition_text` | string | oui |
| `is_day` | bool | oui |

Observation : `observed_at_utc` non nullable, issu en priorité de
`current.last_updated_epoch`. Prévision : `target_at_utc` (epoch source),
`target_local_date`, `lead_time_hours` (float64),
`target_is_past_at_collection` (bool), tous non nullables ;
`chance_of_rain_pct` et `chance_of_snow_pct` (int16) nullables.

Les choix de champs obligatoires sont des **règles du contrat v1**, pas une
promesse de non-nullabilité de WeatherAPI. Les deux captures réelles des 17 et 18 octobre 2026 confirment
les types et valeurs des champs consommés, sans démontrer toutes les variantes
futures. Le profil complet versionné indique les nulls et fréquences observés. Une métrique facultative absente devient null ; une métrique
présente mais invalide entraîne un rejet explicite. Une chaîne numérique peut
être convertie si elle est non ambiguë ; booléens comme nombres, NaN, infinis,
valeurs vides et fractions pour champs entiers sont refusés. Humidité/chances et
nuages : [0,100] ; degrés : [0,360] ; pression >0 ; pluie et vent >=0.
Pas de valeur météo zéro inventée et pas d'interpolation en Silver.

## Identité des villes et temps

La section « Registre des villes » de `silver_rules` versionne trois identifiants stables : `fr_paris`,
`gb_london`, `be_brussels`. `city_by_query()` résout les alias de requête ;
`resolve_city(query, location)` vérifie pays, fuseau IANA et coordonnées dans
une zone métropolitaine contrôlée. Le libellé du fournisseur n'est jamais clé.
Une ville inconnue ou une résolution géographique incohérente est rejetée.
Ajouter une ville demande une évolution explicite du registre.

L'epoch prime sur les heures locales ; les anciennes dates Bronze naïves sont
UTC selon leur contrat existant. Un repli vers une heure locale est possible
seulement si le fuseau donne un instant unique : les heures ambiguës ou
inexistantes sont rejetées. Une journée DST peut compter 23 ou 25 heures ;
le bilan mesure la couverture effective sans imposer 24. Le collecteur demande
désormais trois dates locales, jour courant inclus, et trace cette demande dans
`forecast_request.days` ; les archives antérieures ne portent pas ce champ.
Les sept jours demandés dans les anciennes captures n'en avaient retourné que
trois. Le bilan conserve toujours les jours/heures réellement reçus, sans en
inventer ni appliquer la configuration actuelle aux historiques.

## Statuts, rejets et bilan

| Statut | Signification |
| --- | --- |
| `success` | Au moins une ligne, villes/sections attendues valides, aucun rejet |
| `partial` | Au moins une ligne valide et des sections/villes/lignes manquantes ou rejetées |
| `rejected` | Aucune ligne valide, JSON/enveloppe invalide ou données métier inexploitable |
| `empty` | Aucune ligne, collecte explicitement sans succès ; `summary.reason=collection_failed` |

`Rejection(table, code, city_query, position, field, message)` est sérialisable
par `to_dict()`. Les champs sont strings ou null, jamais une exception SDK ni
une copie de charge brute. Le writer ajoute identité, chemin et empreinte à
chaque diagnostic. Les codes incluent `invalid_json`, `invalid_envelope`,
`summary_mismatch`, `source_path_mismatch`, `unknown_city`, `location_mismatch`,
`missing_field`, `invalid_type`, `invalid_timestamp`, `out_of_range`,
`duplicate_conflict`, `ambiguous_local_time`, `nonexistent_local_time`.

Le résultat conserve provenance et versions ; le bilan S2 reste JSON-safe et décrit les villes demandées,
succès/échecs de collecte, tables et compteurs par ville, horizons, champs
ignorés et nombre de rejets. Les compteurs par table distinguent `input_rows`,
`valid_rows`, `rejected_rows`, `duplicates_removed` et `conflicting_rows`.
Des doublons identiques sont réduits ; toutes les lignes d'un groupe de même
clé avec contenu incompatible sont rejetées. Un total de rejets n'est pas un
total de lignes car un diagnostic peut concerner une section complète.

## Stockage et barrière de publication

Deux racines Delta communes aux ingestions sous
`datalake/silver/weatherapi/schema=v1/transform=1.0.0/`. Pas de partition
physique initiale et pas de table Delta par ingestion. Ne jamais écrire dans
les Parquet sous-jacents ni les lire par glob. Ne pas activer automatiquement
l'évolution de schéma, le compactage ou `VACUUM`.

Le moteur testé crée des tables avec **minReaderVersion=1 et
minWriterVersion=2**, sans fonctionnalité Delta avancée. Les types et la
nullabilité reviennent identiques après relecture Delta/PyArrow en test local.
Une publication S3 immuable référence les versions des **deux** tables après
leurs transactions et l'écriture des rejets. Delta n'a pas de transaction
commune aux deux tables. Gold fige les publications achevées et les versions,
puis filtre leurs identifiants ; il ignore les ingestions partiellement écrites.
Une modification du résultat impose une nouvelle version de transformation et
un préfixe séparé.

## Fixtures réelles et anomalie fournisseur confirmée

Capture en lecture seule de deux JSON Bronze existants, sans nouvel appel
WeatherAPI : [run GitHub 39176102671](https://github.com/mLR5/dataeng-portfolio/actions/runs/39176102671).
Les ingestions source sont datées des **17 et 18 octobre 2026**.

Sous `tests/fixtures/weatherapi/`, `bronze_success_*_derived_real.json` conserve
les valeurs originales et tronque uniquement chaque tableau horaire aux deux
premières heures de chacun des trois jours. La mise en forme JSON est changée.
Les fichiers `.provenance.json` indiquent chemin et SHA-256 **originaux**, SHA-256
du **dérivé**, réduction exacte et résultats attendus. Le dérivé n'est pas
présenté comme le blob original. `real_bronze_profiles.json` conserve les types,
nulls et cardinalités calculés sur les charges **complètes** avant réduction.
Les fichiers `synthetic_minimal*` sont séparément marqués synthétiques.

Constats sur les deux réponses complètes :

- Trois jours réellement reçus, **24 heures par jour et par ville**, soit
  **72 heures par ville**, malgré les sept jours demandés par le collecteur.
- `temp_c`, `feelslike_c`, `pressure_mb`, `wind_kph`, `precip_mm` sont des floats ;
  epochs, pourcentages, degrés, `is_day` et `condition.code` sont des entiers ;
  `condition.text` et les libellés de temps sont des strings.
- Aucun null dans ces champs sur ces deux échantillons ; cela ne prouve pas
  leur non-nullabilité dans toutes les futures réponses.
- **Anomalie source : `Londres` est résolue vers l'Argentine**, fuseau
  `America/Argentina/Catamarca`, et non vers London. Les fixtures gardent cette
  anomalie intacte. Le registre rejette cette ville avec `location_mismatch` ;
  Paris et Bruxelles sont acceptées. Le collecteur utilise désormais des
  coordonnées explicites pour Londres (voir [contrat source Bronze](bronze-ingestion.md#localisation-et-horizon-fournisseur)) ;
  ne jamais renommer les anciennes valeurs argentines en London.

La régression versionnée S1 confirme la géographie erronée et les empreintes.
Les extraits doivent donner à S2 un résultat `partial`, **2 observations et
12 prévisions** ; les sources complètes doivent donner **2 observations et
144 prévisions**. La couverture horaire des extraits est volontairement
incomplète et ne mesure pas l'horizon initial du fournisseur.

## Validation

```sh
python -m pip install -r requirements-databricks.txt
python -m pytest databricks/tests -o testpaths=databricks/tests
```

Les tests chargent les notebooks tels que Databricks les exécute (`%run` compris)
et vérifient types, nullabilité, timestamps UTC à la microseconde, alias des
villes et provenance d'un résultat rejeté. L'écriture Delta réelle (création,
`MERGE` insert-only rejoué sans doublon, relecture) est couverte par
`databricks/tests/test_pipeline_delta.py`, exécuté dans
`.github/workflows/test-silver.yml`, qui échoue si un test Delta est ignoré.

Historique : S1 avait validé ce contrat avec delta-rs, puis S3 l'avait accepté sur
ADLS. Avant le retrait de delta-rs, la CI de la PR #10 a montré que Spark produit
exactement les mêmes lignes que le runner S3 sur les Bronze dérivés de captures
réelles. L'acceptation Azure du job Databricks est la phase 4 de
[l'ADR 0001](adr/0001-silver-databricks-serverless.md).
