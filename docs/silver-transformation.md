# Transformation Silver WeatherAPI

`shared.silver_transform.transform_bronze(raw: bytes, source_blob: str)` transforme
les octets exacts d'un seul JSON Bronze en deux tables Arrow, conformes aux
schémas explicites de S1. Cette fonction est pure : elle ne lit aucun stockage,
ne rappelle pas WeatherAPI et ne réalise aucune écriture Delta. S3 consomme son
`TransformResult` et assure les transactions/publications.

## Règles de données

- `observation_snapshots` provient uniquement de `current` ; une prévision passée
  reste une prévision. Aucun trou d'observation n'est interpolé.
- Les clés incluent `ingestion_id` et `city_id`. L'observation inclut son instant
  source ; la prévision inclut son instant cible. Deux ingestions restent deux
  snapshots même si elles partagent les mêmes valeurs ou la même heure cible.
- Les epochs sont prioritaires pour les instants UTC. En leur absence, un horaire
  local est résolu avec le fuseau IANA du registre ; un horaire ambigu ou inexistant
  au changement d'heure est rejeté. Une date locale n'est pas une date UTC.
- Le timestamp de collecte historique sans fuseau est interprété en UTC, comme
  dans Bronze. La référence doit correspondre à l'identifiant et à sa partition
  horaire UTC. Les chemins signés/URLs sont refusés comme arguments.
- Le registre vérifie la ville demandée, le pays, les coordonnées et le fuseau.
  Les identifiants ne sont jamais créés depuis un libellé fournisseur libre.
- La température, l'identité et l'instant sont requis. Les métriques facultatives
  absentes restent nulles. Une valeur fournie invalide rejette sa ligne ; elle
  n'est pas transformée silencieusement en zéro ou en null.
- Les chaînes décimales non ambiguës sont acceptées. Les pourcentages/degrés
  doivent être entiers ; NaN, infinis et coercition de booléens en nombres sont
  interdits. Humidité, nuages et probabilités restent dans `[0,100]`, direction
  du vent dans `[0,360]`, vitesse/précipitations sont non négatives et la pression
  fournie doit être strictement positive. Une pression absente reste nulle.
  Une température numérique extrême n'est pas rejetée par une borne arbitraire.
- Les doublons internes identiques sont réduits à une ligne. Un groupe de lignes
  incompatibles pour une même clé est entièrement rejeté : la sélection ne dépend
  pas de l'ordre des lignes source.
- Les champs météo non utilisés sont ignorés, mais leurs noms apparaissent dans
  `summary.ignored_fields` pour suivre l'évolution de la source. Le Bronze conserve
  leurs valeurs originales.

## Provenance et résultats

Chaque ligne porte la version de schéma/transformation, la référence relative
Bronze et le SHA-256 des **octets lus**, sans normalisation du JSON. Le résultat
conserve également cette provenance si aucune ligne valide n'est produite.
Les rejets contiennent des codes stables, une table/section, une ville, un champ
et une position source. Ils ne recopient pas les valeurs rejetées ni les erreurs
SDK. Ils sont sérialisables avec `Rejection.to_dict()`.
Un libellé de ville contenant un surrogate Unicode invalide rejette l'enveloppe
avec un diagnostic fixe ; son texte ne peut pas contaminer les rejets UTF-8.

| Statut | Signification |
| --- | --- |
| `success` | Au moins une ligne valide, aucun rejet ; les doublons identiques ne sont pas des rejets |
| `partial` | Des lignes valides et au moins un rejet ou une ville dont la collecte a échoué |
| `rejected` | JSON/enveloppe incohérent, ou collecte déclarée réussie mais aucune ligne exploitable |
| `empty` | Aucune ville collectée avec succès, motif explicite `collection_failed` |

Un défaut de métrique dans `current` n'empêche pas de publier les prévisions
valides de la ville. Une heure invalide ne supprime pas les autres heures.
Les erreurs d'arguments du programme ou les pannes techniques restent des
exceptions ; elles ne deviennent pas des rejets métier terminaux.

Le bilan expose `tables` et `city_tables` avec `input_rows`, `valid_rows`,
`rejected_rows`, `duplicates_removed` et `conflicting_rows`. Pour les lignes
candidates d'une ville géographiquement validée :

```text
input_rows = valid_rows + rejected_rows + duplicates_removed
```

Les échecs de collecte, villes non reconnues et sections invalides ont leur
diagnostic distinct ; ils ne constituent pas des heures météo inventées et ne
sont pas comptés comme lignes candidates. `conflicting_rows` est un sous-ensemble
de `rejected_rows`. Les demandes/villes échouées proviennent de l'enveloppe
historique, pas de la configuration actuelle de Bronze.

`horizons[city_id]` décrit les jours/heures réellement reçus, les cibles min/max,
les horizons min/max et la couverture de chaque journée locale. Une journée DST
de 23 ou 25 heures a respectivement 23 ou 25 créneaux attendus. La couverture
manquante est informative : aucune heure ni aucun septième jour ne sont inventés.

## Vérification

Après installation des dépendances S1 et des dépendances de tests existantes :

```sh
python -m pytest tests/test_silver_quality.py tests/test_silver_transform.py
```

Les cas synthétiques vérifient les valeurs/schémas Arrow, la provenance exacte,
UTC/DST, les métriques nulles et invalides, les rejets partiels, les doublons,
les conflits, l'échec total et la conservation des révisions entre ingestions.
Le lot S2 comprend 75 cas : suite S2 passée localement sur Python 3.12 avec
PyArrow 21.0.0 ; 242 tests passent avec les 167 tests Bronze existants.
Les deux fixtures `*_derived_real.json` de S1 conservent les valeurs de vrais
Bronze des 17 et 18 octobre 2026, avec seulement deux heures conservées par jour.
Leurs fichiers de provenance distinguent le SHA original et le SHA des extraits.
Chaque extrait produit deux observations et douze prévisions ; la vérification
locale des deux JSON originaux complets produit deux observations et 144
prévisions par ingestion, soit 288 snapshots conservés pour 144 cibles distinctes.

Ces captures révèlent un défaut source : la requête `Londres` a été résolue vers
l'Argentine (`America/Argentina/Catamarca`). Le registre rejette cette ville avec
`location_mismatch`, garde Paris/Bruxelles et retourne `partial`, bien que Bronze
signale trois appels HTTP réussis. Les fixtures et les historiques restent
intacts : aucune donnée argentine n'est renommée en observation londonienne.
L'horizon réellement reçu est de trois jours, 72 heures par ville valide.

La collecte courante demande désormais trois dates locales et utilise des
coordonnées explicites pour les trois villes. Son champ additionnel
`forecast_request.days` est compatible avec le transformateur ; il ne remplace
pas la mesure des jours et heures réellement présents dans la réponse et ne
change pas le traitement des archives historiques.

Les tests unitaires n'établissent pas la compatibilité ADLS ni le chargement du
paquet Azure Functions : leur acceptation relève de S1/S3 puis de S4 sur Azure.
