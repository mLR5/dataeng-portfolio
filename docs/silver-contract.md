# Silver WeatherAPI v1 — contrats et preuve de faisabilité Delta

Contrat stabilisé pour les lots S1/S2/S3. Source : JSON Bronze déjà archivé ;
aucun appel WeatherAPI par Silver. Deux tables Delta, sans table quotidienne
supplémentaire. Les exemples synthétiques servent aux tests ; seules les fixtures
accompagnées d'une provenance réelle constituent des observations du fournisseur.

## Versions et interface communes

- `functions/shared/silver_contract.py` est l'unique définition des schémas,
  clés, versions et dataclasses.
- `SCHEMA_VERSION=1`, `TRANSFORM_VERSION="1.0.0"`, `SOURCE="weatherapi"`.
- Python cible : **3.11 Linux**, identique à Azure Functions. Les tests locaux
  S1 ont tourné sous Python **3.12.14** ; la CI dédiée exécute Python 3.11.
- Dépendances isolées dans `requirements-silver.txt` : **deltalake 1.2.1**
  (delta-rs) et **PyArrow 21.0.0**. Le paquet Functions Bronze n'est pas modifié
  par S1 ; son adoption et sa validation au déploiement relèvent de S4.
- `transform_bronze(raw: bytes, source_blob: str) -> TransformResult` est
  implémenté par S2 dans `shared.silver_transform` sans I/O.
- S3 consomme `result.tables`, `TABLE_SCHEMAS`, `TABLE_KEYS`, `result.rejections`
  et `result.summary`. S1 ne dépend ni du writer ni du transformateur.

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

`shared.silver_cities` versionne trois identifiants stables : `fr_paris`,
`gb_london`, `be_brussels`. `city_by_query()` résout les alias de requête ;
`resolve_city(query, location)` vérifie pays, fuseau IANA et coordonnées dans
une zone métropolitaine contrôlée. Le libellé du fournisseur n'est jamais clé.
Une ville inconnue ou une résolution géographique incohérente est rejetée.
Ajouter une ville demande une évolution explicite du registre.

L'epoch prime sur les heures locales ; les anciennes dates Bronze naïves sont
UTC selon leur contrat existant. Un repli vers une heure locale est possible
seulement si le fuseau donne un instant unique : les heures ambiguës ou
inexistantes sont rejetées. Une journée DST peut compter 23 ou 25 heures ;
le bilan mesure la couverture effective sans imposer 24. Les sept jours
**demandés** à WeatherAPI ne garantissent pas sept jours **reçus** : le bilan
conserve jours/heures réellement observés, sans en inventer.

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
  Paris et Bruxelles sont acceptées. La correction de la requête d'origine est
  un chantier Bronze distinct ; ne jamais renommer ces valeurs en London.

La régression versionnée S1 confirme la géographie erronée et les empreintes.
Les extraits doivent donner à S2 un résultat `partial`, **2 observations et
12 prévisions** ; les sources complètes doivent donner **2 observations et
144 prévisions**. La couverture horaire des extraits est volontairement
incomplète et ne mesure pas l'horizon initial du fournisseur.

## Validation exécutée et limites

Commande S1 :

```sh
python -m pip install -r requirements-dev.txt -r requirements-silver.txt
python -m pytest tests/test_silver_contract.py -q
```

Résultat local S1 : **24 tests nouveaux, 191 tests avec la suite Bronze**
sous Python3.12.14 ; `pip check` réussit. Les tests créent réellement les deux tables Delta sur filesystem local,
initialement vides, insèrent des lots typés, les relisent et réalisent un
`MERGE` insert-only rejoué sans doublon. Ils vérifient timestamp UTC à la
microseconde, dates, types, nullabilité, alias des villes et provenance d'un
résultat rejeté. Ils n'utilisent aucune doublure pour delta-rs.

Le workflow Bronze existant ignore les modules `tests/test_silver*.py` pour
conserver son environnement de dépendances indépendant. Le workflow
`.github/workflows/test-silver.yml` teste toutes les PR, y compris empilées,
et exécute la suite sur Python3.11 Linux et
`pip check`. La présence du workflow n'est pas une preuve de son exécution ;
le statut réel du run GitHub doit être vérifié après publication.

**Restent à valider sur Azure en S4** : build distant complet et découverte des
Functions Bronze/Silver, taille/mémoire du paquet, authentification ADLS réelle,
lease et perte de lease, concurrence/transactions sur object store, reprise
entre tables et lecture Gold. Le succès local ne prouve pas ces garanties ADLS.
Aucune table Silver métier Azure n'est créée et aucune modification de données
Azure n'est réalisée par S1.
