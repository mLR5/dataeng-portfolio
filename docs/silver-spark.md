# Silver sur Databricks serverless

Décision et contexte : [ADR 0001](adr/0001-silver-databricks-serverless.md).
Bronze reste dans la Function App ; Silver devient un job Databricks quotidien
qui réutilise le contrat S1 et le transformateur S2, et conserve les garanties S3.

## Fonctionnement

Le job `silver-weatherapi` a deux tâches, une seule exécution à la fois :

1. **publish** (`silver-run`) liste Bronze (`weather/weatherapi/**/*_weather_data.json`),
   retire les ingestions présentes dans `publications`, lit les octets exacts des
   fichiers restants (`binaryFile`), applique `transform_bronze`, puis écrit par lot :
   observations, prévisions, quarantaine, et enfin publications.
2. **freshness** (`silver-freshness`) échoue si la dernière collecte publiée a plus
   de 26 h ou si les dernières 24 h comptent moins de 20 publications.

```text
datalake/<silver_path>/
  observation_snapshots/            table Delta, clé ingestion_id, city_id, observed_at_utc
  forecast_hourly_snapshots/        table Delta, clé ingestion_id, city_id, target_at_utc
  _control/quarantine/              table Delta, clé ingestion_id, rejection_index
  _control/publications/            table Delta, clé ingestion_id : registre d'avancement
```

Les quatre tables sont `delta.appendOnly=true`, sans deletion vectors, et restent
lisibles par delta-rs 1.2.1 (testé en CI). Aucun checkpoint, curseur ou lease :
la table `publications` dit ce qui est fait.

| Garantie S3 | Mise en œuvre Spark |
| --- | --- |
| Insert-only, pas de mise à jour de snapshot | `MERGE ... WHEN NOT MATCHED THEN INSERT *` sur les clés du contrat, tables append-only |
| Une ligne existante différente est refusée | `exceptAll` sur la ligne entière avant et après le `MERGE` (`SnapshotConflict`) |
| Hash Bronze différent sous un ID publié | `PublicationConflict`, visible en mode `--reconcile` |
| Publication après les deux tables | Écrite en dernier ; Gold filtre sur les publications complètes |
| Reprise après panne entre tables | Les lignes déjà écrites sont comparées égales, rien n'est réinséré |
| Un seul écrivain | `max_concurrent_runs: 1` + isolation optimiste Delta ; un conflit fait échouer la tâche, rejouée |

Les contrôles communs (identité Bronze, cohérence statut/lignes, provenance,
JSON canonique, quarantaine) sont dans `functions/shared/silver_checks.py`,
utilisé à la fois par le runner delta-rs et par Spark.

## Lecture Gold

```python
from silver_spark.gold import published_read_plan, read_published
plan = published_read_plan(spark, SilverPaths(root), collected_before=cutoff)
forecasts = read_published(spark, plan, "forecast_hourly_snapshots")
```

Le plan fige les IDs publiés et la version maximale de chaque table ; la lecture se
fait à cette version (`versionAsOf`) avec un filtre sur ces IDs. Ne jamais lire les
Parquet par glob.

## Tests

```sh
pip install -r requirements-dev.txt -r requirements-silver.txt -r requirements-spark.txt
python -m pytest spark/tests -o testpaths=spark/tests
```

Sans accès Maven, les tests Delta sont ignorés localement ; la CI
(`test-silver-spark.yml`) échoue si l'un d'eux est ignoré. Elle compare aussi la
sortie Spark à celle du runner S3 sur les Bronze dérivés de captures réelles.

## Déploiement (phases 2 à 5)

Rien n'est créé tant que la variable GitHub `DATABRICKS_ENABLED` vaut `false`
(défaut).

1. **Socle Azure** : `DATABRICKS_ENABLED=true`, puis le déploiement infra crée le
   workspace serverless (`azapi`, `computeMode: Serverless`), l'Access Connector et
   ses rôles : lecture sur `bronze`, écriture sur `datalake`.
2. **Unity Catalog** : workflow *Databricks Silver*, `uc-plan` puis `uc-apply`
   (storage credential, external locations `weather-bronze` en lecture seule et
   `weather-silver`, droits du principal CI).
3. **Validation** (cible `validation`, racine `silver/_validation/spark`, planning en
   pause) : `bundle-deploy` puis `bundle-run`. Comparer avec
   `scripts/silver_backfill.py` sur une racine séparée, relancer pour vérifier
   l'absence de nouveau commit, puis lire `system.billing.usage` pour le coût.
4. **Production** (cible `production`, racine
   `silver/weatherapi/schema=v1/transform=1.0.0`, tous les jours à 02 h UTC) :
   `bundle-deploy -t production` après la validation, puis 7 jours verts.

Points à vérifier pendant la validation, faute de pouvoir les prouver hors Azure :
disponibilité des jobs serverless en francecentral, `environment_version: "3"` et
installation de la wheel, accès `binaryFile` aux external locations, propriétés de
table imposées par Databricks.

## Exploitation

- Relancer après une panne : relancer le job, sans rien effacer.
- Bronze modifié sous un ID publié : la tâche échoue avec `PublicationConflict` ;
  enquêter, ne pas supprimer la publication.
- Changer une règle de transformation : nouvelle `TRANSFORM_VERSION`, donc nouvelle
  racine, jamais de modification en place.
