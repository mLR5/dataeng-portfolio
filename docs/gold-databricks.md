# Gold WeatherAPI sur Databricks

Le premier lot Gold construit les quatre dimensions et les six faits du modèle
v4 à partir de Silver v2. Il utilise les notebooks source `.py` existants comme
mode de livraison, le même compute serverless et le workflow **Deploy
Infrastructure**. Le contrat détaillé et les décisions manquantes de la v3
sont explicités dans [gold-contract.md](gold-contract.md).

Les agrégats `agg_city_daily`, les vues du dashboard,
`features_city_hourly` et MLflow appartiennent aux étapes suivantes.

## Notebooks et job

| Notebook | Rôle |
| --- | --- |
| `gold_contract` | Schémas, clés et version du contrat Gold |
| `gold_dimensions` | Dimensions, état de collecte par ville, observation horaire canonique |
| `gold_forecasts` | Prévisions horaires et quotidiennes, erreurs face aux observations disponibles |
| `gold_pipeline` | Assemblage des composants, lecture Silver publiée, contrôles et publication Gold |
| `gold_build` | Point d'entrée du job et widgets d'exécution |

Les cinq notebooks sont déployés sous `/Shared/weather-silver/`, aux côtés
des notebooks Silver. Les `%run` relatifs utilisent ainsi les mêmes composants
que les tests et le job. Le plan distinct de conversion des points d'entrée en
`.ipynb` n'est pas appliqué par ce lot.

Le job conserve son nom `silver-weatherapi`, son horaire quotidien de 02 h UTC,
sa limite globale de deux heures et une seule exécution simultanée. Après une
publication Silver réussie, deux tâches peuvent s'exécuter :

```text
publish ─┬─ freshness
         └─ gold_build
```

`gold_build` dépend de `publish`, sans dépendre du contrôle d'activité récente
`freshness`. Il dispose de deux retries avec un intervalle minimal de cinq
minutes. Une erreur Gold fait échouer le job ; les notifications e-mail suivent
la variable GitHub `DATABRICKS_ALERT_EMAILS` déjà existante.

Terraform transmet `silver_root`, `gold_root`, `uc_schema` et
`full_rebuild_weekday = "6"` au notebook. La racine de production Gold 2.0.0 est
`abfss://datalake@<compte>.dfs.core.windows.net/gold/weatherapi_v2` ; le schéma
est `<projet>_<env>.gold`. La racine 1.0.0 `gold/weatherapi` reste sur le stockage,
n'est plus alimentée et peut servir de comparaison : le contrat interdit de
mélanger deux versions dans une racine. Le catalogue, le schéma Gold,
l'emplacement externe `gold/` et les droits du principal existaient déjà.

L'enregistrement Unity Catalog suit la même règle que Silver : un nom déjà
enregistré ailleurs (la racine 1.0.0) est **déplacé**. Seule la définition de la
table EXTERNAL est supprimée, jamais ses fichiers, puis la table est recréée sur
le nouveau chemin. Une table MANAGED du même nom est refusée. Le premier run
après déploiement construit `weatherapi_v2` en complet, puis déplace les dix noms.

## Tables et publication

Les tables Delta externes sont stockées sous `<gold_root>/<table>` :

- `dim_city`, `dim_date`, `dim_condition`, `dim_collection` ;
- `fact_collection_city`, `fact_observation_hourly` ;
- `fact_forecast_hourly`, `fact_forecast_hourly_error` ;
- `fact_forecast_daily`, `fact_forecast_daily_error`.

Les tables configurent le clustering défini par table sur la ville et/ou le
temps, avec les deletion vectors désactivés. La table `fact_forecast_daily`
porte la contrainte Delta `lead_days_range` : `lead_days BETWEEN -1 AND 2`.

Le contrôle `<gold_root>/_control/builds` enregistre les publications Gold,
leurs identifiants sources, le mode de chargement et les versions Delta des tables
et des contrôles. Il est écrit en dernier, une fois les sorties construites et
contrôlées. Deux petites tables de contrôle servent l'incrémental :
`_control/city_locations` (coordonnées validées par ingestion et ville, source de
la SCD 2) et `_control/condition_label_counts` (effectifs des libellés, source de
la SCD 1).

La source est l'ensemble figé des ingestions Silver marquées `complete`, y
compris les publications `partial`. Une publication partielle peut contenir
des données valides pour Paris et Bruxelles malgré un rejet géographique de
Londres. Filtrer uniquement les statuts `success` retirerait cet historique.

Chaque écriture Gold (remplacement ou MERGE) est atomique par table, mais dix
écritures ne constituent pas une transaction multi-table. La lecture directe
des noms Unity Catalog offre l'atomicité par table. Les consommateurs qui
exigent un état cohérent entre toutes les tables doivent utiliser le manifeste
publié et les fonctions `gold_read_plan` puis `read_gold` de `gold_pipeline` :
elles lisent les versions Delta du même build achevé.

Une exécution interrompue avant le manifeste ne publie pas de nouveau build.
Les versions de l'ancien build restent lisibles. Relancer le job pour reconstruire
les sorties, sans effacer les contrôles ni modifier les snapshots Silver.

## Incrémental et périodes explicites

`gold_build` expose `start_date`, `end_date`, `mode` et `full_rebuild_weekday`,
en plus des racines et du schéma Unity Catalog. Les dates sélectionnent les
**collectes sources UTC**, avec début inclus et fin exclue. Une demande du
`2026-11-10` au `2026-11-13` sélectionne les collectes des 10, 11 et 12 novembre.

- `mode=incremental` (défaut) ajoute les nouvelles ingestions sélectionnées à
  l'ensemble déjà publié dans le manifeste et les charge par MERGE. Sans nouvelle
  ingestion, il n'écrit rien.
- `mode=full` recalcule toutes les tables sur l'ensemble cumulatif et les
  remplace : c'est la référence de l'incrémental.
- `full_rebuild_weekday` (0 = lundi … 6 = dimanche, UTC ; vide = désactivé) force
  `mode=full` ce jour-là, même sans nouvelle ingestion. Le job le fixe à `6` :
  filet de sécurité hebdomadaire, sur le modèle de `full_scan_weekday` de Silver.
- Une période explicite ne retire jamais les ingestions des builds précédents.
  Pour construire une sortie limitée à une période, utiliser une racine Gold
  distincte et vide, ainsi qu'un schéma de validation adapté.

Le premier build d'une racine est toujours complet. Ensuite, chaque table suit la
stratégie décrite dans [gold-contract.md](gold-contract.md#stratégie-de-chargement) :
insertion seule pour les dimensions de collecte, le calendrier et les prévisions,
SCD 2 pour `dim_city`, SCD 1 pour `dim_condition`, upsert restreint à la fenêtre
affectée pour l'observation canonique et les deux tables d'erreurs. La fenêtre,
par ville, vaut `[min(collected_at_utc) - 1 jour, max(collected_at_utc) + 3 jours]`
des nouvelles ingestions, élargie à leurs cibles ; les observations Silver de la
fenêtre, avec une marge d'une heure, sont relues pour recalculer les heures
canoniques. La qualité (couverture, alertes d'ordre des températures) est calculée
sur les tables Gold, petites, et non sur tout Silver.

Le build bascule seul en `full` (`load_reason = city_history_changed`) si une
collecte tardive ou une modification du registre change une version de ville qui
commence avant la dernière collecte déjà consommée. Le rapport et le manifeste
indiquent `load_mode` (`full` ou `incremental`) et la raison.

Une exécution interrompue laisse des MERGE partiels hors manifeste. La relance est
sûre : les insertions ignorent les clés présentes, les upserts recalculent la
fenêtre et les compteurs SCD 1 sont réécrits en valeur absolue depuis le build
précédent épinglé.

Le registre Gold est lié à sa source et à sa version de contrat. Un changement
des règles doit suivre une migration explicite vers une sortie dédiée ; il ne
faut pas mélanger des constructions issues de contrats différents.

## Validation et exploitation

Les tests utilisent les notebooks effectivement déployés, chargés par le
mécanisme `%run` de `databricks/tests/conftest.py`. Le workflow existant
`test-silver.yml` exécute les tests Silver et Gold ; il échoue si les tests Delta
sont ignorés faute de JARs Maven.

```sh
pip install -r requirements-databricks.txt
python -m pytest databricks/tests -q -o testpaths=databricks/tests
```

Les contrôles portent notamment sur les clés, les relations et l'équivalence
du calcul complet avec plusieurs lots incrémentaux pour le même ensemble de
sources (`test_gold_incremental_delta.py` : quatre lots MERGE comparés ligne à
ligne, historique `dim_city` et contrôles compris, avec une observation tardive
plus proche de l'heure, une vérité arrivée après ses prévisions et un changement
de coordonnées ; lot vide sans commit ; tables à insertion seule jamais réécrites ;
recalcul hebdomadaire identique). Les cas de changement d'heure, ex æquo, données absentes et collecte
tardive doivent rester couverts. Les erreurs quotidiennes exigent une vérité
disponible sur le jour local complet de 23, 24 ou 25 heures ; les températures
issues d'observations horaires décrivent cet échantillonnage, pas des extrema
physiques mesurés en continu.

`gold_quality` produit deux indicateurs, retournés dans le rapport d'exécution
et enregistrés dans `quality_json` du manifeste :

- `observation_coverage` donne, pour chaque ville ayant des observations, la
  première et la dernière heure canonique, le nombre d'heures attendues entre
  ces bornes incluses, le nombre d'heures observées et le taux de couverture.
  Le dénominateur porte sur **tout l'historique disponible de la ville**, et non
  sur chaque mois ni sur une période précédant sa première observation valide.
- `daily_temperature_order_alerts` compte les prévisions quotidiennes dont les
  températures connues ne respectent pas `min ≤ moyenne ≤ max`. Cet indicateur
  est une alerte et ne bloque pas, à lui seul, la publication.

Le widget `minimum_coverage_pct` de `gold_build` vaut **98** par défaut pour le
job. Une ville sous ce seuil fait échouer le contrôle avant l'écriture du nouveau
manifeste. Les écritures physiques des tables restent soumises à la distinction
entre lecture directe et lecture cohérente par manifeste décrite plus haut.
L'API `run_gold` utilise un défaut de **0**, adapté aux tests et aux appels de
bibliothèque sur de petits échantillons ; un appel de production doit conserver
le seuil explicite du notebook ou en fournir un autre volontairement. Une
validation sur un échantillon discontinu peut renseigner
`minimum_coverage_pct=0` pour désactiver ce seuil, tout en conservant les autres
contrôles et les indicateurs de qualité.

Pour la première validation dans Databricks, choisir une racine isolée sous
`datalake/gold/` et un schéma dédié. Vérifier le premier calcul, sa relance,
l'équivalence avec un cumul de lots et la lecture du dernier manifeste avant de
diriger des consommateurs vers la sortie de production. La présence du code
ou un déploiement Terraform réussi ne remplace pas cette validation d'exécution.

Le contrôle `freshness` conserve son rôle sur le flux Silver récent ; il ne
mesure pas la complétude d'un rattrapage historique. Les échecs d'une tâche ou
les journaux du build Gold sont à consulter dans le même job Databricks.

Un seuil de couverture positif refuse également un build sans aucune observation.
Il exige une couverture mesurable pour Paris et Bruxelles, conformément au
critère v4 ; Londres est évaluée sur son historique valide lorsqu'il existe.
Le clustering utilise la commande SQL Delta `ALTER TABLE ... CLUSTER BY` :
l'API générique Python peut ignorer le clustering lors d'une écriture par chemin.
