# Silver sur Databricks serverless

Décision et contexte : [ADR 0001](adr/0001-silver-databricks-serverless.md).
La Function App n'ingère que Bronze ; tout Silver vit dans des notebooks
Databricks, déployés par le Terraform de `infra/` et le workflow *Deploy
Infrastructure* existant.

## Notebooks

| Notebook | Rôle |
| --- | --- |
| `silver_rules` | Contrat des tables, registre des villes, qualité, `transform_bronze`, contrôles de cohérence. Python pur |
| `silver_pipeline` | `%run ./silver_rules`, puis schémas Spark, écriture Delta insert-only, découverte Bronze, lecture Gold, noms Unity Catalog, fraîcheur |
| `silver_publish` | Tâche 1 du job : widgets, publication des ingestions en attente, puis enregistrement des noms Unity Catalog stables (widget `uc_schema`) |
| `silver_freshness` | Tâche 2 du job : échoue si la dernière collecte publiée a plus de 26 h ou si les 24 dernières heures comptent moins de 20 publications |
| `silver_explore` | Lecture seule : publications par jour et statut, rejets fréquents (par portée), contrôles v2, prévisions horaires et quotidiennes lues comme Gold |

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
datalake/<racine Silver>/            silver/weatherapi_v2 pour le modèle v2
  observation_snapshots/            clé ingestion_id, city_id, observed_at_utc
  forecast_hourly_snapshots/        clé ingestion_id, city_id, target_at_utc
  forecast_daily_snapshots/         clé ingestion_id, city_id, target_local_date
  _control/quarantine/              clé ingestion_id, rejection_index
  _control/publications/            clé ingestion_id : registre d'avancement
```

Les cinq tables Delta sont `delta.appendOnly=true`, sans deletion vectors, et
restent lisibles par delta-rs 1.2.1 (testé en CI). Aucun checkpoint, curseur ou
lease : la table `publications` dit ce qui est fait.

Ordre d'écriture d'un lot : observations, prévisions horaires, prévisions
quotidiennes, quarantaine, puis publications, strictement en dernier.

La quarantaine porte la colonne `scope` : `row` (la ligne est retirée) ou `field`
(une mesure optionnelle invalide est mise à NULL, la ligne est gardée). Son
empreinte `quarantine_sha256` couvre cette colonne.

Colonnes de contrôle de `publications` (en plus de la version Delta et du nombre
de lignes de chacune des trois tables : `observation_*`, `forecast_*`,
`forecast_daily_*`) :

| Colonne | Contenu |
| --- | --- |
| `bronze_format` | `current` ou `legacy` (enveloppe historique convertie) ; `current` si le résumé ne le dit pas (JSON illisible) |
| `requested_horizon_days` | Horizon demandé à WeatherAPI (`forecast_request.days`), NULL s'il est absent |
| `nulled_metrics` | Nombre de rejets `scope = field`, donc de mesures mises à NULL |
| `unknown_path_count`, `unknown_paths_json` | Chemins Bronze sans décision dans le registre (alerte, pas d'évolution de schéma) |

| Garantie | Mise en œuvre |
| --- | --- |
| Insert-only, pas de mise à jour de snapshot | `MERGE ... WHEN NOT MATCHED THEN INSERT *` sur les clés du contrat, tables append-only |
| Une ligne existante différente est refusée | `exceptAll` sur la ligne entière avant et après le `MERGE` (`SnapshotConflict`) |
| Bronze modifié sous un ID publié | `PublicationConflict`, visible avec le widget `reconcile=true` |
| Publication après les trois tables | Écrite en dernier, après la quarantaine ; Gold filtre sur les publications complètes |
| Reprise après panne entre tables | Les lignes déjà écrites sont comparées égales, rien n'est réinséré |
| Un seul écrivain | `max_concurrent_runs = 1` + isolation optimiste Delta ; un conflit fait échouer la tâche, rejouée |

## Lecture Gold

```python
# MAGIC %run /Shared/weather-silver/silver_pipeline
plan = published_read_plan(spark, SilverPaths(silver_root), collected_before=cutoff)
forecasts = read_published(spark, plan, FORECAST_TABLE)
daily = read_published(spark, plan, FORECAST_DAILY_TABLE)
```

Le plan fige les IDs publiés et la version maximale de chacune des trois tables ;
la lecture se fait à cette version (`versionAsOf`) avec un filtre sur ces IDs. Ne
jamais lire les Parquet par glob.

## Noms stables Unity Catalog

Avec le widget `uc_schema` (Terraform le règle à `<projet>_<env>.silver`), `silver_publish`
appelle `register_tables` après une exécution réussie : chacune des cinq tables
devient une table EXTERNAL `<catalogue>.<schéma>.<table>` sur son chemin Delta.
Les lecteurs utilisent `<projet>_<env>.silver.<table>` (sortie Terraform
`databricks_silver_schema`) et n'ont plus à connaître la racine. Le catalogue
porte le préfixe projet/environnement car le metastore régional est partagé
par tous les workspaces : un nom `weather` nu pourrait entrer en collision.
Terraform crée le catalogue (stockage `datalake/uc`), les schémas `silver` et
`gold`, l'emplacement externe `datalake/gold` et les droits du principal.

- Idempotent : un nom déjà enregistré au bon emplacement n'est pas touché.
- Nouvelle racine (par exemple `silver/weatherapi` vers `silver/weatherapi_v2`) :
  la définition est supprimée puis recréée sur le nouveau chemin. Supprimer une
  table EXTERNAL ne supprime pas ses fichiers ; une table MANAGED du même nom est
  refusée (`CatalogConflict`), jamais supprimée.
- `uc_schema` doit être exactement `catalogue.schéma` (lettres, chiffres, `_`),
  validé avant toute écriture ; vide, aucun nom n'est enregistré.
- Le schéma Unity Catalog doit exister, et l'identité du job doit pouvoir créer
  une table externe sur l'emplacement externe Silver (`CREATE EXTERNAL TABLE`).
- Entre la suppression et la recréation d'un nom déplacé, un lecteur peut ne pas
  le trouver : relancer la lecture.

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
| `DATABRICKS_SILVER_PATH` | `silver/weatherapi_v2` (production) | Racine Silver dans `datalake` ; `silver/weatherapi` garde la v1 pour comparaison |
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

Les workflows déploient la production par défaut : racine `silver/weatherapi_v2`
(Silver v2), planning actif. La v1 reste sous `silver/weatherapi`, en lecture,
jusqu'à la validation de la comparaison v1/v2 (`compare-silver-v1-v2.yml`, sur
la branche `validation/silver-v2-compare`). Le premier run remplit tout l'historique ; les suivants n'ajoutent
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
