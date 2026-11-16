# Gold WeatherAPI : données du dashboard

Ce lot prépare la couche de lecture de l'étape 5 du modèle WeatherAPI v4 :
`agg_city_daily` et les vues `v_current_weather`,
`v_latest_forecast_hourly` et `v_latest_forecast_daily`. Les pages du dashboard,
le choix entre Databricks AI/BI et Power BI, les variables ML et MLflow restent
des travaux distincts.

Il prolonge les notebooks Gold existants, sans nouveau notebook ni changement
du job Terraform. Le point d'entrée reste `gold_build`. Le contrat des dix tables
de l'étape 4 et `GOLD_VERSION = "1.0.0"` restent inchangés. La couche de lecture
possède sa propre version, `DASHBOARD_VERSION = "1.0.0"`.

Le développement est isolé dans une branche Gold. La présence du code et les
tests locaux ne valent pas déploiement ni validation d'exécution en production.
Une modification concurrente du Silver doit être rapprochée de cette branche
avant son intégration.

## Sources et publication

La couche de lecture dérive d'un **build Gold achevé**. Elle lit les versions
Delta du manifeste Gold, et non les versions courantes prises séparément pendant
un recalcul. Le registre Gold existant sous `_control/builds` garde son format et
son rôle pour les dix tables de base.

La publication du dashboard utilise un registre distinct sous
`_control/dashboard_builds`. Ce registre associe la version du traitement de
lecture, le build Gold source, ses versions de tables, la version Delta de
`agg_city_daily`, le schéma Unity Catalog et une empreinte des définitions des
vues. Les vues utilisent les tables Gold
nommées dans Unity Catalog avec les versions du build source épinglées ; leur
horloge de consultation reste dynamique.

L'agrégat est une table Delta externe sous `<gold_root>/agg_city_daily`. Son
remplacement est atomique à l'échelle de cette table. La création ou le
remplacement de plusieurs vues s'effectue successivement : ce n'est pas une
transaction regroupant les trois vues et l'agrégat. Le registre du dashboard ne
doit être écrit qu'après validation et publication des objets concernés. Les
consommateurs exigeant la cohérence de tous les objets doivent suivre le build
publié et ses versions, au lieu de déduire cette cohérence des seuls noms Unity
Catalog. La publication suppose un seul writer par racine Gold ; il ne faut pas
lancer plusieurs publications concurrentes sur la même racine, y compris depuis
deux jobs différents.

L'API `run_gold_dashboard(spark, gold_root=..., uc_schema=..., force=False)` est
appelée par `gold_build` après la construction du Gold de base et son éventuel
enregistrement Unity Catalog. Elle ne modifie ni les dix tables de base ni leur
registre. Un `uc_schema` vide construit la table physique et son registre sans
créer de table ou de vue dans Unity Catalog.

Avant de remplacer les sorties, un précontrôle vérifie les objets du schéma
Unity Catalog. Les tables existantes doivent pointer vers les chemins attendus.
Une vue existante ne peut être remplacée que si sa propriété
`weatherapi.dashboard` correspond à `DASHBOARD_VERSION`. Un nom occupé par un
autre type d'objet ou une vue sans cette propriété provoque un refus, au lieu
d'écraser un objet étranger.

Si le build Gold source, le schéma cible et l'empreinte attendue des vues sont
inchangés, que l'agrégat physique porte toujours la version publiée et que tous
les objets Unity Catalog requis existent, une relance avec `force=False` ne
réécrit rien et renvoie `changed=False`. Un agrégat absent ou dont la version a
changé, un enregistrement Unity Catalog manquant pour `agg_city_daily` ou une
vue manquante déclenchent
une reconstruction et une republication, même sans nouveau build Gold source.

Ce contrôle ne compare pas le texte réel des vues avec le SQL attendu : une
modification manuelle de la définition d'une vue existante peut donc ne pas être
détectée. `force=True` reconstruit et republie la couche de lecture pour rétablir
les définitions attendues. Les contrôles de propriété et de chemin restent
appliqués même avec `force=True` ; ce paramètre n'autorise pas l'écrasement d'un
objet étranger.

`gold_dashboard_read_plan(spark, gold_root)` retourne le dernier build achevé,
son build Gold source, le chemin et la version Delta de l'agrégat, et les chemins
et versions des tables Gold sources. Une lecture reproductible de l'agrégat
utilise ce plan :

```python
plan = gold_dashboard_read_plan(spark, gold_root)
aggregate = (
    spark.read.format("delta")
    .option("versionAsOf", plan["aggregate"]["version"])
    .load(plan["aggregate"]["path"])
)
```

Si une écriture ou un DDL échoue avant le nouveau registre, le plan du dernier
build achevé reste disponible. Les noms de tables ou vues peuvent déjà refléter
une partie de la tentative ; il faut utiliser les versions du plan pour lire
l'état publié, puis relancer la publication.

Les versions Delta référencées doivent rester disponibles. Une opération
`VACUUM` qui les supprime invaliderait les lectures de ces snapshots ; la
publication d'un manifeste ne constitue pas une politique de conservation.

## Agrégat quotidien par ville

Le schéma exécutable est `AGG_CITY_DAILY_SCHEMA` dans
[gold_contract.py](../databricks/notebooks/gold_contract.py). La clé est
`(city_id, local_date)`. La fonction de calcul est
`build_gold_daily_aggregate` dans
[gold_dimensions.py](../databricks/notebooks/gold_dimensions.py).

Pour une ville ayant des observations canoniques, le calendrier commence à la
plus ancienne date locale connue entre sa première collecte contenant une donnée
météo valide et sa première observation canonique, puis se termine à sa dernière
date locale observée. Une observation encore datée de la veille lors d'une
première collecte après minuit est ainsi conservée. Si la date de première
collecte manque, les observations établissent le début. Une ville sans observation n'a pas
de ligne dans cet agrégat ; elle reste présente dans `dim_city`. Les jours
intermédiaires sans observation restent visibles. Il n'y a ni observation
inventée ni extension de l'historique avant le premier jour valide connu d'une
ville, notamment pour Londres.

Avant publication, un contrôle rapproche le nombre d'heures observées de
l'agrégat et celui des observations Gold sources, par ville et date locale.
Une heure manquante ou ajoutée empêche la publication du nouveau registre.

| Colonnes | Définition |
| --- | --- |
| `city_id`, `local_date` | Identifiant stable et date dans le fuseau IANA de la ville |
| `expected_hours` | Nombre d'heures UTC entre deux minuits locaux : 23, 24 ou 25 |
| `observed_hours` | Nombre d'heures UTC canoniques observées ce jour |
| `coverage_pct`, `is_complete` | Couverture des créneaux attendus ; complet seulement si tous sont présents |
| `temperature_observed_hours`, `temperature_min_c`, `temperature_max_c`, `temperature_avg_c` | Nombre et statistiques des températures horaires disponibles |
| `humidity_observed_hours`, `humidity_avg_pct` | Nombre et moyenne des humidités disponibles |
| `wind_observed_hours`, `wind_avg_kph`, `wind_max_kph` | Nombre, moyenne et maximum des vitesses de vent disponibles |
| `wind_gust_observed_hours`, `wind_gust_max_kph` | Nombre et maximum des rafales disponibles |
| `precipitation_observed_hours`, `precipitation_total_mm`, `precipitation_is_complete` | Nombre et somme des valeurs de précipitation rapportées ; indicateur distinct de couverture complète |
| `sunrise_utc`, `sunset_utc`, `daylight_hours` | Paire solaire issue d'une prévision du même jour, et durée entre les deux instants |

La couverture utilise toujours une journée locale entière, y compris le premier
et le dernier jour partiellement collectés. Elle n'est pas ramenée à 100 % sur
les seules heures déjà écoulées. Au retour à l'heure d'hiver, deux observations
portant le même numéro d'heure locale restent deux créneaux UTC distincts.

Un jour sans observation a des compteurs à zéro et des statistiques météo à
`NULL`. Une métrique absente n'est pas remplacée par zéro. `is_complete` porte
sur la présence des observations, pas sur la disponibilité de chacune de leurs
métriques : les compteurs par métrique rendent cette distinction explicite.

Les températures sont celles d'échantillons horaires, pas des extrema physiques
mesurés en continu. `precipitation_total_mm` additionne les valeurs rapportées
par les observations disponibles ; ce n'est pas une garantie de cumul continu
d'une station. Son indicateur de complétude et son nombre d'échantillons doivent
accompagner son affichage, en particulier pour une journée partielle.

Pour le solaire, le traitement retient la prévision la plus récente contenant
une **paire complète et cohérente** pour la date locale concernée : les deux
instants appartiennent à ce jour et le coucher ne précède pas le lever. Si une
prévision plus récente est incomplète, il se replie sur une autre prévision du
même jour. Il ne mélange pas le lever d'une émission et le coucher d'une autre.
Sans paire valide, les deux instants et la durée restent `NULL`.

Ce repli peut utiliser une émission reçue après le jour observé. L'agrégat
décrit l'historique disponible au build ; il ne constitue pas une table de
variables ML garantissant ce qui était connu à un instant passé.

## Vues de lecture

Les définitions proviennent de `gold_dashboard_view_queries` dans
[gold_forecasts.py](../databricks/notebooks/gold_forecasts.py). Elles exposent
les champs météo de leur fait source, les informations de la ville, les
identifiants de provenance, `as_of_utc` et un âge explicite. Elles ne déclenchent
aucun appel à WeatherAPI.

| Vue | Sélection |
| --- | --- |
| `v_current_weather` | Dernière observation publiée par ville, avec instant observé et collecte au plus tard à l'instant de consultation |
| `v_latest_forecast_hourly` | Dernière émission disponible par ville et cible horaire encore future ou égale à l'instant de consultation |
| `v_latest_forecast_daily` | Dernière émission disponible par ville et date cible locale à partir d'aujourd'hui, avec `lead_days >= 0` |

La sélection horaire utilise un instant UTC exact : une cible à 14 h est retirée
après 14 h, même si l'heure civile 14 h–15 h est encore en cours. La sélection
quotidienne utilise la date actuelle dans le fuseau de chaque ville, et non la
date UTC de la session.

Une émission postérieure à la consultation est exclue. Pour une même cible, le
classement privilégie `issued_at_utc` décroissant, puis l'identifiant d'ingestion
pour départager les ex æquo. La météo courante privilégie l'instant observé le
plus récent, puis l'instant de collecte et l'identifiant d'ingestion. Une ville
sans observation éligible ne produit pas de ligne dans `v_current_weather`.

`observation_age_hours` mesure l'âge de l'observation, pas celui de l'exécution
du job. `is_stale` devient vrai au-delà de **26 heures**, seuil lié à la cadence
quotidienne actuelle de publication Gold. Ce seuil ne signifie pas qu'une donnée
vieille de 25 heures est une mesure en direct. Les vues de prévision exposent
`forecast_age_hours`, calculé depuis l'émission retenue.

Les versions des données sont figées par publication, tandis que
`current_timestamp()` est évalué lors de chaque consultation. Des lignes de
prévision peuvent donc disparaître quand leur cible passe, sans nouvelle
écriture Gold. Pour les tests reproductibles, le générateur SQL accepte un
`as_of` de type `datetime` avec fuseau ; il n'accepte pas d'expression SQL
arbitraire comme horloge.

## Travail concurrent avec Silver

Le partage des fichiers permet un développement parallèle :

| Lot Gold | Lot Silver / job |
| --- | --- |
| `gold_contract.py`, `gold_dimensions.py`, `gold_forecasts.py`, `gold_pipeline.py`, `gold_build.py` | Notebooks et modules `silver_*`, orchestration Silver |
| Tests Gold et documentation Gold | Tests Silver et documentation Silver |
| Aucun changement Terraform ou CI requis par ce lot | Évolutions éventuelles du job et de son déploiement, à rapprocher avant intégration |

Des branches ou worktrees distincts évitent les écritures concurrentes dans un
même fichier. Ils ne garantissent pas la compatibilité des données. Le contrat
à préserver comprend les tables Silver, leurs noms et types de colonnes, les
clés, les unités, les fuseaux, les règles de publication et les versions Delta
référencées par le registre `publications`.

Le code Gold charge aussi `silver_pipeline` par `%run` et réutilise ses
interfaces, notamment `SilverPaths`, `_version`, `_delta`, `_same_location`, les
exceptions de publication/catalogue et les constantes de contrat. Une refonte
de ces fonctions ou de leurs signatures peut casser le Gold même avec des tables
Silver identiques. Le lot Silver doit conserver ces interfaces, ou prévoir
l'adaptation Gold lors du rapprochement des branches.

Une réorganisation du job ou des fonctions Silver qui conserve ce contrat peut
être intégrée indépendamment. Un changement de schéma, de clé, d'identité de
ville, de sélection des publications ou de règle métier demande une adaptation
et un rejeu comparatif Gold. Les changements doivent être rapprochés et testés
sur la même révision avant un déploiement du job partagé. Les contrôles Gold
de l'étape 4 restent requis ; l'ajout de vues ne remplace pas ces contrôles.

## Validation

Les tests utilisent les notebooks existants via le même chargement `%run` que
le reste de la suite Databricks. Les cas propres à cette couche comprennent :

- jours locaux de 23, 24 et 25 heures, heures répétées, journées absentes et
  journées de début ou de fin incomplètes, observation datée de la veille de
  la première collecte et rapprochement des heures avec le Gold source ;
- métriques partiellement ou entièrement nulles, compteurs par métrique et
  absence d'imputation ;
- repli solaire vers une paire complète du même jour, sans assemblage de deux
  émissions ni invention lorsque la source est absente ;
- dernière émission par cible, exclusion des émissions futures et des jours
  quotidiens à délai négatif, frontière de date locale et fraîcheur ;
- lecture des versions épinglées après une nouvelle écriture de la table source,
  compatibilité des vues persistantes et reprise de publication ;
- équivalence entre un build complet et le même ensemble de sources admis par
  plusieurs lots, ainsi que relance sans écriture inutile.

### Résultats locaux du 16 novembre 2026

La suite Databricks complète a réussi : **573 tests, aucun échec ni test
ignoré**, en 788,03 s, sur la première implémentation
[`02b78b7`](https://github.com/mLR5/dataeng-portfolio/commit/02b78b7ae98f71b977289cedd93298b594444d10).
Après la correction du calendrier initial et l'ajout du rapprochement des
heures sources, les **36 tests ciblés du lot** ont été réexécutés avec succès,
sans test ignoré, en 189,80 s. Ces nombres correspondent à deux exécutions avec
des tests communs ; ils ne s'additionnent pas.

La dernière passe couvre aussi la stabilité des trois vues persistantes pour
un client en `America/New_York`, l'arrêt d'une publication qui perd des heures,
et sa réparation. Environnement : Spark 4.0.5, Delta Spark 4.0.1, Python 3.12
et Java 21. Les tests écrivent de vraies tables Delta locales ; aucune donnée
Azure n'est modifiée.

```bash
python -m pytest databricks/tests -q -o testpaths=databricks/tests -rs
python -m pytest databricks/tests/test_gold_daily_aggregate.py databricks/tests/test_gold_dashboard_pipeline.py databricks/tests/test_gold_dashboard_views.py -q -rs
```

La validation d'exécution sur Databricks doit utiliser une racine Gold
et un schéma Unity Catalog isolés. Il reste ensuite à vérifier les droits de
lecture avec l'identité du futur outil de dashboard, puis à réaliser les pages.
En particulier, la persistance des clauses `VERSION AS OF` dans les vues doit
être validée sur le runtime Databricks cible avant le déploiement partagé.
