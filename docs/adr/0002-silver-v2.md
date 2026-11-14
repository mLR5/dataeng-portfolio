# ADR 0002 — Silver v2 : table quotidienne, mesures enrichies et non-omission

Statut : acceptée (novembre 2026). Complète l'[ADR 0001](0001-silver-databricks-serverless.md).

## Contexte

Le modèle Gold v3 (schéma en étoile, tableau de bord puis usages ML) s'appuie
sur des champs Bronze que Silver v1 ignorait : prévisions quotidiennes
(`forecastday[].day`, `astro`), UV, rafales, visibilité, point de rosée, neige,
températures ressenties et de bulbe humide. Un audit a aussi montré que la v1
perdait des données valides :

- 10 prévisions rejetées car une probabilité décimale (55.80025) était refusée
  par un contrat entier ;
- 20 archives historiques à l'ancien format (`ingestion_metadata`) rejetées
  entièrement, soit environ 33 observations et 2 336 prévisions ;
- une mesure facultative invalide supprimait toute la ligne.

## Décision

- `SCHEMA_VERSION=2`, `TRANSFORM_VERSION="2.0.0"`, nouvelle racine
  `silver/weatherapi_v2` ; la v1 reste en place pour comparaison.
- Nouvelle table `forecast_daily_snapshots` (clé `ingestion_id, city_id,
  target_local_date`) avec `lead_days`, agrégats journaliers, lever et coucher
  du soleil (texte local et instant UTC calculé avec le fuseau IANA).
- Mesures ajoutées aux observations et aux prévisions horaires ; probabilités en
  `float64` dans [0,100], sans arrondi.
- Rejet au niveau du champ : une mesure facultative invalide devient nulle et
  produit un rejet `scope=field` ; identité, ville, instants et température
  restent bloquants (`scope=row`). Le statut `partial` ne reflète que les rejets
  de ligne.
- Adaptateur de l'ancien format Bronze (`normalize_envelope`), sans inventer
  d'identité : les contrôles d'identité, de géographie et de temps sont inchangés.
- Registre de non-omission (`silver_registry`) : chaque chemin Bronze a une
  décision ; un chemin inconnu est publié (`unknown_paths`) comme alerte, sans
  évolution de schéma automatique.
- Noms Unity Catalog stables `<projet>_<env>.silver.<table>` enregistrés par le
  job, pour que Gold ne dépende pas de la racine physique.
- Le notebook de règles est découpé en neuf notebooks chaînés par `%run`.

## Exclusions documentées

Champs lunaires, `is_sun_up`/`is_moon_up`, unités impériales, `wind_dir`,
`condition.icon`, `will_it_*` horaires et les champs `current` partiels
(`chance_*`, `will_it_*`). Le registre donne la raison de chaque exclusion ; le
Bronze les conserve.

## Conséquences

- Le premier run sur `silver/weatherapi_v2` rejoue tout l'historique Bronze
  (environ 45 minutes en v1).
- Bascule acceptée seulement si la comparaison v1/v2 (`compare-silver-v1-v2.yml`)
  montre : toutes les clés v1 présentes en v2, valeurs communes identiques, lignes
  supplémentaires expliquées (ancien format, probabilités décimales), environ
  3 lignes quotidiennes par ville et collecte, aucun chemin inconnu, aucune clé
  en double.
- Après 7 jours verts, `silver/weatherapi` (v1) pourra être supprimé.
