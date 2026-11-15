# Contrat Gold WeatherAPI : dimensions et faits

Version du traitement : `GOLD_VERSION = "1.0.0"`. Schémas exécutables, nullabilité,
clés et relations : [gold_contract.py](../databricks/notebooks/gold_contract.py).

Ce contrat implémente l'étape 4 du modèle WeatherAPI v4 : quatre dimensions et six
faits. `agg_city_daily`, les vues du dashboard et `features_city_hourly` appartiennent
aux étapes suivantes. Il utilise Silver v2 sans modifier ses règles ni ses snapshots.

Le document v4 renvoie à une v3 non présente dans le dépôt pour plusieurs détails.
Les choix complémentaires ci-dessous sont donc explicités ; ils ne sont pas
présentés comme une transcription de règles v3 qui auraient été vérifiées.

## Source et identité

- Lire uniquement les ingestions Silver dont la publication est `complete`, y
  compris les statuts `partial`, `empty` et `rejected`. Une ingestion partielle
  peut fournir des données valides pour certaines villes.
- Figer les identifiants de publication et les versions Delta des tables Silver
  avant le calcul. Ne pas lire les fichiers Parquet directement.
- Les identifiants de ville proviennent du registre Silver. Un libellé de requête
  ou le nom renvoyé par l'API n'est jamais une clé.
- `issued_at_utc` est l'instant de collecte Silver, pas l'instant d'exécution du job.
- Les instants sont des timestamps UTC. Les dates et heures locales utilisent le
  fuseau IANA de la ville ; `local_hour` vaut de 0 à 23 et ne constitue pas une clé.
- Les champs météo gardent leurs noms et unités Silver : degrés Celsius, km/h,
  kilomètres, millibars, millimètres et centimètres ; probabilités en DOUBLE,
  exprimées entre 0 et 100.

## Tables et clés

| Table | Clé | Rôle |
| --- | --- | --- |
| `dim_city` | `city_id` | Référentiel des villes et première collecte ayant une ligne valide |
| `dim_date` | `date` | Calendrier des dates référencées par les faits et les collectes |
| `dim_condition` | `condition_code, is_day` | Condition, libellé représentatif et catégorie |
| `dim_collection` | `ingestion_id` | Origine, instant, statut et compteurs de la publication Silver |
| `fact_collection_city` | `ingestion_id, city_id` | État et compteurs de qualité de chaque ville à chaque collecte |
| `fact_observation_hourly` | `city_id, hour_utc` | Une observation canonique par heure UTC et par ville |
| `fact_forecast_hourly` | `city_id, target_hour_utc, issued_at_utc` | Toutes les émissions horaires strictement futures |
| `fact_forecast_hourly_error` | `city_id, target_hour_utc, issued_at_utc` | Erreur de température quand une observation canonique existe |
| `fact_forecast_daily` | `city_id, target_local_date, issued_at_utc` | Prévisions quotidiennes officielles WeatherAPI |
| `fact_forecast_daily_error` | `city_id, target_local_date, issued_at_utc` | Comparaison aux températures horaires échantillonnées d'une journée complète |

Les clés sont naturelles et contrôlées lors de la construction. Les relations sont
décrites par `GOLD_FOREIGN_KEYS` et vérifiées avant la publication Gold ; leur
présence dans le contrat ne prétend pas que Delta impose des clés étrangères.

## Dimensions et suivi des collectes

`dim_city` contient les villes du registre, avec nom d'affichage stable, pays et
fuseau. Latitude et longitude viennent d'une ligne Silver valide ; elles restent
nulles si aucune ligne valide n'existe. `first_collected_at_utc` mesure la première
collecte avec une ligne valide dans au moins une table météo, pas la première
requête adressée à l'API. Ainsi Londres n'acquiert pas artificiellement un historique
valide en janvier à partir de réponses géographiquement rejetées.

`dim_date.day_of_week` suit ISO : lundi = 1, dimanche = 7 ; les libellés du calendrier
sont en français. Les dates locales cibles et observées sont couvertes, en plus
des dates UTC de collecte.

`dim_condition` inclut les couples présents dans les observations, les prévisions
horaires et les prévisions quotidiennes (ces dernières avec `is_day = true`). Un
couple quotidien est ajouté même s'il est absent des prévisions horaires de la
période sélectionnée. Le libellé le plus fréquent est retenu, avec départage
lexical ; `label_fr` reflète les libellés français fournis par la source, sans
traduction automatique. Il reste nullable si aucun libellé n'est disponible. Un
code non répertorié reçoit la catégorie `unknown` et `is_precipitation = NULL`.
Une condition absente reste absente : pas de code météo inventé pour combler un NULL.

`dim_collection` conserve toutes les publications achevées. `cities_requested` et
`cities_successful` sont des nombres de villes, pas des listes d'identifiants ; les
libellés du résumé sont résolus avec le registre pour construire le fait par ville.
Un instant de collecte absent reste nullable et n'est pas remplacé par l'heure du job.

`fact_collection_city` distingue :

- `collected` : présence d'une requête, d'une réponse ou de lignes météo, sans
  échec explicite de collecte ni rejet géographique ;
- `collection_failed` : échec explicite de collecte dans le résumé ou la quarantaine ;
- `location_rejected` : réponse reçue mais identité géographique refusée ;
- `not_requested` : ville du référentiel absente des requêtes de cette collecte.

Le statut `collected` ne garantit pas qu'une ligne météo a passé les contrôles :
une réponse reçue avec toutes ses lignes invalides garde ce statut, avec compteurs
de lignes valides à zéro. La qualité et la disponibilité des mesures se lisent
dans ces compteurs et dans les rejets, séparément du succès de collecte.

Les compteurs `observation_valid`, `forecast_hourly_valid` et `forecast_daily_valid`
sont des nombres de lignes valides. `forecast_hourly_expected` utilise les créneaux
attendus des jours effectivement reçus, via `horizons.<city>.daily_coverage` ; il
ne multiplie pas aveuglément `requested_horizon_days` par 24. L'ancien format peut
annoncer sept jours demandés tout en n'en contenant que trois. `rejected_rows`
compte les lignes rejetées des tables météo dans `city_tables` ; `nulled_metrics`
compte les rejets de champ de la quarantaine. Un échec d'enveloppe n'est pas
comptabilisé comme une ligne météo fictive.

## Observation canonique et conditions facultatives

L'heure canonique est `date_trunc('hour', observed_at_utc + INTERVAL 30 MINUTES)`.
Les observations candidates d'une même ville et d'une même heure sont départagées
par distance croissante à l'heure canonique, puis instant de collecte croissant,
instant observé croissant, identifiant d'ingestion et empreinte de source dans
l'ordre lexical. À distance égale, la capture la plus ancienne reste prioritaire.
`distance_to_hour_min` conserve l'écart absolu entre l'heure canonique et l'instant
effectivement observé.

La date et l'heure locales sont celles de **l'heure canonique**, pas celles de
l'instant brut : une observation à 23 h 45 peut appartenir au jour suivant après
arrondi. Les répétitions d'heure locale au changement d'heure gardent deux clés
UTC distinctes.

Une relation de condition n'est résolue que si son code est disponible et son
couple jour/nuit est défini. En quotidien, `is_day` reste `true` même quand le code
facultatif est absent. Les valeurs météo manquantes ne sont pas remplacées par zéro.

## Prévisions et erreurs

Le fait horaire conserve uniquement `lead_time_hours > 0`. Il garde toutes les
émissions d'une même cible : choisir la dernière prévision appartient aux futures
vues du dashboard. Une nouvelle collecte ne remplace pas les anciennes émissions.

La prévision quotidienne conserve `lead_days` de -1 à 2 inclus. `is_day = true`
permet sa relation avec la dimension de condition. Les textes et instants de lever
et coucher du soleil restent nullable. La contrainte de délai quotidien ne devient
pas une règle d'exclusion des 115 lignes historiques à -1.

L'erreur horaire associe une émission future à l'observation canonique de sa ville
et de son heure cible. Elle garde les identifiants des deux collectes et l'instant
réellement observé. Le signe est **prévision moins observation** :

```text
temperature_error_c          = forecast_temperature_c - observed_temperature_c
absolute_temperature_error_c = abs(temperature_error_c)
squared_temperature_error_c  = temperature_error_c * temperature_error_c
```

Une prévision sans observation reste dans le fait de prévision et n'a pas encore
de ligne d'erreur. L'arrivée tardive de son observation peut donc produire une
nouvelle erreur lors d'une exécution ultérieure.

L'erreur quotidienne ne prend que `lead_days >= 0`. Sa vérité est calculée sur les
températures des observations canoniques horaires d'une date locale. Elle n'est
publiée que si tous les créneaux UTC attendus de cette journée sont représentés :
23, 24 ou 25, selon le fuseau et le changement d'heure. Un compte de 24 heures
locales distinctes ne convient pas aux journées de changement d'heure.

**Les minima et maxima observés sont ceux d'échantillons horaires.** Ils ne sont
pas des extrema continus mesurés sur la journée ; même avec couverture complète,
un pic entre deux observations peut manquer. Les colonnes
`observed_temperature_min_c`, `observed_temperature_max_c` et
`observed_temperature_avg_c` portent cette définition. Les erreurs correspondantes
gardent le signe prévision moins observation. Une température quotidienne prévue
absente produit une erreur nulle, sans supprimer les autres comparaisons disponibles.

## Contrôles et reproductibilité

- Clés non nulles et uniques, types et colonnes conformes au contrat.
- Relations résolues pour les références présentes.
- Statuts de collecte connus ; compteurs non négatifs.
- Délai horaire strictement positif ; délai quotidien entre -1 et 2 ; délai
  quotidien d'erreur non négatif.
- Couverture des observations calculée sur une période explicitement définie,
  par ville, sans créer d'observations pour remplir les trous.
- `temperature_min_c ≤ temperature_avg_c ≤ temperature_max_c` constitue une alerte
  quotidienne ; ce contrôle ne modifie pas les températures reçues.
- Un même ensemble figé de publications Silver doit produire le même contenu Gold,
  qu'il soit calculé en une fois ou admis par plusieurs lots incrémentaux.

Les périodes explicites de rattrapage sélectionnent des **collectes UTC**. Leurs
effets peuvent viser des heures ou jours locaux extérieurs à cette période.
L'incrémental doit mettre à jour les observations canoniques affectées et les erreurs
des prévisions anciennes lorsque de nouvelles observations deviennent disponibles.
La stratégie physique et la publication atomique des sorties sont documentées dans
le guide d'exploitation Gold livré avec le pipeline.

## Stockage Delta

Les tables sont externes et leurs fichiers restent sous la racine Gold choisie.
Le writer configure le clustering Delta avec les colonnes de `GOLD_CLUSTER_COLUMNS` :
ville et heure/date pour les faits météo, et clés adaptées pour les dimensions.
Les deletion vectors sont désactivés. Le clustering ne remplace pas les contrôles
de clés et n'ajoute pas de colonnes de partitionnement au contrat. La commande
`ALTER TABLE ... CLUSTER BY` enregistre cette configuration ; elle ne constitue
pas une réorganisation des fichiers déjà écrits. Ce traitement ne lance pas
d'opération `OPTIMIZE` supplémentaire.

`fact_forecast_daily` porte aussi la contrainte Delta persistée
`lead_days_range CHECK (lead_days BETWEEN -1 AND 2)`. Son éventuel commit de
métadonnées est inclus dans la version épinglée par le manifeste Gold. Les noms
et types d'une table déjà écrite doivent correspondre au contrat : une réécriture
n'active pas `overwriteSchema` pour masquer une évolution de schéma. Une évolution
de version se prépare dans une nouvelle racine de sortie.
