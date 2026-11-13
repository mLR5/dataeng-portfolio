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
serverless (mode de performance par défaut), une seule exécution à la fois, 2 retries de la publication, limite de 2 heures.

Chaque nuit, la découverte ne liste que les 3 derniers jours UTC de Bronze : c'est
le listing qui coûte en opérations de stockage. Bronze entier est listé au premier
run (aucune publication), chaque dimanche pour rattraper une archive tardive, en
mode `reconcile`, ou si la fenêtre ne correspond à aucune partition. Widgets du
notebook : `lookback_days` (0 = tout) et `full_scan_weekday` (0 = lundi).

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
| `DATABRICKS_SILVER_PATH` | `silver/weatherapi` (production) | Racine Silver dans `datalake` ; `silver/_validation/spark` a servi à la validation |
| `DATABRICKS_SCHEDULE_PAUSED` | `false` (production) | `true` suspend le job quotidien de 02 h UTC |
| `DATABRICKS_ALERT_EMAILS` | `[]` | Liste JSON d'adresses notifiées en cas d'échec ; à définir en variable GitHub (dépôt public : pas d'adresse dans le code) |

Le provider Databricks utilise le même principal de service que `azure/login`
(secret `AZURE_CREDENTIALS`), qui devient administrateur du workspace qu'il crée.

### Validation (phase 4, 12 novembre 2026)

Sur `silver/_validation/spark` : 7 151 archives publiées en 47 minutes, relance en
2 min 30 qui n'a ajouté que la collecte arrivée entre-temps, aucune archive
manquante ni publication en double. Coût Azure de la journée : 4,42 $ CAD de
Databricks (calcul serverless et stockage) et environ 0,90 $ de lectures/écritures
ADLS supplémentaires.

### Production (phase 5)

Les workflows déploient la production par défaut : racine `silver/weatherapi`,
planning actif. Le premier run remplit tout l'historique ; les suivants n'ajoutent
que les nouvelles collectes. Après 7 jours verts, supprimer `silver/_validation/spark`
et les branches `validation/*` de cette migration.

## Exploitation

- Relancer après une panne : relancer le job, sans rien effacer.
- Bronze modifié sous un ID publié : la tâche échoue avec `PublicationConflict` ;
  enquêter, ne pas supprimer la publication.
- Changer une règle de transformation : nouvelle `TRANSFORM_VERSION` et nouvelle
  racine (par exemple `silver/weatherapi_v2`), jamais de modification en place :
  les tables sont insert-only et refusent une ligne existante différente.
- Modifier un notebook : commit sur `main` ; *Deploy Infrastructure* republie le
  notebook (Terraform compare son contenu).
