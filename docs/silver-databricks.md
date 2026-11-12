# Silver sur Databricks serverless

Décision et contexte : [ADR 0001](adr/0001-silver-databricks-serverless.md).
La Function App n'ingère que Bronze ; tout Silver vit dans des notebooks
Databricks, déployés par le Terraform de `infra/` et le workflow *Deploy
Infrastructure* existant.

## Notebooks

| Notebook | Rôle |
| --- | --- |
| `silver_rules` | Contrat des tables, registre des villes, qualité, `transform_bronze`, contrôles de cohérence. Python pur |
| `silver_pipeline` | `%run ./silver_rules`, puis schémas Spark, écriture Delta insert-only, découverte Bronze, lecture Gold, fraîcheur |
| `silver_publish` | Tâche 1 du job : widgets, puis publication des ingestions en attente |
| `silver_freshness` | Tâche 2 du job : échoue si la dernière collecte publiée a plus de 26 h ou si les 24 dernières heures comptent moins de 20 publications |
| `silver_explore` | Lecture seule : publications par jour et statut, rejets fréquents, prévisions lues comme Gold |

Ils sont publiés dans le workspace sous `/Shared/weather-silver/`. Le job
`silver-weatherapi` enchaîne `silver_publish` puis `silver_freshness` sur compute
serverless, une seule exécution à la fois, avec 2 retries de la publication.

## Données

```text
datalake/<racine Silver>/
  observation_snapshots/            clé ingestion_id, city_id, observed_at_utc
  forecast_hourly_snapshots/        clé ingestion_id, city_id, target_at_utc
  _control/quarantine/              clé ingestion_id, rejection_index
  _control/publications/            clé ingestion_id : registre d'avancement
```

Les quatre tables Delta sont `delta.appendOnly=true`, sans deletion vectors, et
restent lisibles par delta-rs 1.2.1 (testé en CI). Aucun checkpoint, curseur ou
lease : la table `publications` dit ce qui est fait.

| Garantie | Mise en œuvre |
| --- | --- |
| Insert-only, pas de mise à jour de snapshot | `MERGE ... WHEN NOT MATCHED THEN INSERT *` sur les clés du contrat, tables append-only |
| Une ligne existante différente est refusée | `exceptAll` sur la ligne entière avant et après le `MERGE` (`SnapshotConflict`) |
| Bronze modifié sous un ID publié | `PublicationConflict`, visible avec le widget `reconcile=true` |
| Publication après les deux tables | Écrite en dernier ; Gold filtre sur les publications complètes |
| Reprise après panne entre tables | Les lignes déjà écrites sont comparées égales, rien n'est réinséré |
| Un seul écrivain | `max_concurrent_runs = 1` + isolation optimiste Delta ; un conflit fait échouer la tâche, rejouée |

## Lecture Gold

```python
# MAGIC %run /Shared/weather-silver/silver_pipeline
plan = published_read_plan(spark, SilverPaths(silver_root), collected_before=cutoff)
forecasts = read_published(spark, plan, FORECAST_TABLE)
```

Le plan fige les IDs publiés et la version maximale de chaque table ; la lecture se
fait à cette version (`versionAsOf`) avec un filtre sur ces IDs. Ne jamais lire les
Parquet par glob.

## Tests

```sh
pip install -r requirements-databricks.txt
python -m pytest databricks/tests -o testpaths=databricks/tests
```

`databricks/tests/conftest.py` exécute les fichiers notebooks tels que Databricks
les lance, `%run` compris : on teste le code déployé, pas une copie. Sans accès
Maven, les tests Delta sont ignorés localement ; la CI (`test-silver.yml`) échoue
si l'un d'eux est ignoré.

## Déploiement et variables GitHub

Tout passe par *Deploy Infrastructure* (push sur `main` ou lancement manuel).

| Variable GitHub | Défaut | Effet |
| --- | --- | --- |
| `DATABRICKS_ENABLED` | `true` (défaut des workflows) | `false` ne crée rien, ou supprime ce qui existe ; `true` crée workspace serverless, Access Connector, rôles, objets Unity Catalog, notebooks et job |
| `DATABRICKS_SILVER_PATH` | `silver/_validation/spark` | Racine Silver dans `datalake` ; `silver/weatherapi` en production |
| `DATABRICKS_SCHEDULE_PAUSED` | `true` | `false` active le job tous les jours à 02 h UTC |
| `DATABRICKS_ALERT_EMAILS` | `[]` | Liste JSON d'adresses notifiées en cas d'échec |

Le provider Databricks utilise le même principal de service que `azure/login`
(secret `AZURE_CREDENTIALS`), qui devient administrateur du workspace qu'il crée.

### Phase 4 : validation

1. Databricks est activé par défaut dans les workflows : le merge sur `main` déploie tout.
2. Dans le workspace, lancer le job `silver-weatherapi` à la main (ou le notebook
   `silver_publish` avec ses widgets). Il traite tout l'historique Bronze vers
   `silver/_validation/spark`.
3. Relancer : `published` doit valoir 0 et aucune version Delta ne doit s'ajouter.
4. Contrôler avec `silver_explore`, puis lire le coût dans `system.billing.usage`.

À vérifier pendant cette phase, faute de pouvoir le prouver hors Azure :
disponibilité des jobs serverless en francecentral, droits du principal pour créer
la storage credential au niveau du metastore, accès `binaryFile` aux external
locations, création des tables par chemin, propriétés de table imposées par
Databricks.

### Phase 5 : production

`DATABRICKS_SILVER_PATH=silver/weatherapi`,
`DATABRICKS_SCHEDULE_PAUSED=false`, adresses d'alerte, puis *Deploy
Infrastructure*. Après 7 jours verts, Silver devient la source de Gold ; la racine
de validation peut être supprimée.

## Exploitation

- Relancer après une panne : relancer le job, sans rien effacer.
- Bronze modifié sous un ID publié : la tâche échoue avec `PublicationConflict` ;
  enquêter, ne pas supprimer la publication.
- Changer une règle de transformation : nouvelle `TRANSFORM_VERSION` et nouvelle
  racine (par exemple `silver/weatherapi_v2`), jamais de modification en place :
  les tables sont insert-only et refusent une ligne existante différente.
- Modifier un notebook : commit sur `main` ; *Deploy Infrastructure* republie le
  notebook (Terraform compare son contenu).
