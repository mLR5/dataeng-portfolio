# S4 : alimentation automatique et exploitation Silver Delta

Après fusion et déploiement, `weather_silver_processor` traite Bronze toutes les
cinq minutes et `silver_health_monitor` le contrôle indépendamment toutes les
quinze minutes. S4 ne rappelle pas WeatherAPI et ne supprime aucun fichier Bronze.
Les deux tables et la barrière de publication S3 sont conservées.

## Exécution et état durable

L'état est sous `<SILVER_ROOT>/_control/runtime/coordinator.json`. Le blob JSON
est lui-même protégé par un lease renouvelé ; chaque remplacement porte ce lease,
contrôlé par Azure. Un ancien coordinateur ne peut pas écraser l'état après perte
de propriété. Le lease S3 et les transactions Delta protègent séparément les
écritures métier, y compris face à un backfill lancé en parallèle.

Deux parcours bornés partagent les travaux en attente :

- **Récent** : six dernières heures, pour alimenter rapidement les nouvelles collectes.
- **Historique** : de la première archive canonique retenue jusqu'à une borne UTC
  figée au début de chaque passage, par fenêtres de sept jours. Une fois terminé,
  ce parcours recommence et recherche à nouveau la première archive. Une arrivée
  tardive antérieure à l'ancien point de départ reste donc découverte.

La recherche initiale avance elle aussi par pages, avec un curseur persistant :
plus de mille répertoires ou anciens objets non canoniques ne bloquent pas le
bootstrap. Le premier passage effectue le bootstrap automatiquement. Aucun jour de départ
n'est deviné. `SILVER_START_AT` permet de restreindre explicitement ce périmètre ;
par défaut, toutes les archives canoniques retenues sont éligibles.

Une page découverte est ajoutée aux travaux persistants **dans le même
remplacement atomique que son curseur**, avant toute transformation ou transaction.
Le curseur ne signifie jamais que les fichiers ont été traités. Un travail est
retiré seulement après publication complète ou confirmation de cette publication.
Une panne après publication et avant checkpoint est reprise sans nouveau commit.

Les erreurs techniques restent en attente sans limite de tentatives : délai de
30 secondes, puis doublement jusqu'à une heure. Les erreurs ne contiennent que
leur classe ; aucun message SDK, URL signée ou secret n'est persisté. Une
contention S3 est un résultat `busy`, pas un rejet métier. Les autres candidats
continuent et la moitié de la capacité est réservée à chaque parcours. Quand une
capacité est atteinte, la découverte s'arrête sans avancer son curseur.

La limite de temps est vérifiée entre les découvertes et les ingestions. Une
transaction déjà lancée ne peut pas être annulée proprement ; le timeout Functions
est de dix minutes et la prochaine invocation reprend les travaux durables.

## Configuration

| Paramètre | Défaut | Rôle |
| --- | --- | --- |
| `SILVER_ENABLED` | `true` | Interrupteur du traitement et de la sonde |
| `SILVER_TIMER_SCHEDULE` | `0 */5 * * * *` dans Terraform | Timer de traitement, expression NCRONTAB UTC |
| `SILVER_HEALTH_SCHEDULE` | `0 */15 * * * *` dans Terraform | Timer indépendant de santé |
| `SILVER_ROOT` | `silver/weatherapi/schema=v1/transform=1.0.0` | Tables et contrôles de cette version |
| `SILVER_START_AT` | Première archive retenue | Borne UTC de restriction optionnelle |
| `SILVER_BATCH_SIZE` | `20` | Ingestions tentées par invocation, maximum 100 |
| `SILVER_DISCOVERY_LIMIT` | `40` | Objets inspectés par page de parcours, maximum 100 |
| `SILVER_DISCOVERY_PAGES` | `48` | Appels de pagination par parcours, maximum 168 |
| `SILVER_PENDING_LIMIT` | `500` | Travaux persistants, au plus 1 000, moitié par parcours |
| `SILVER_LOOKBACK_HOURS` | `6` | Fenêtre rapide ; la réconciliation historique reste complète |
| `SILVER_BUDGET_SECONDS` | `240` | Budget entre unités de travail, maximum 480 |
| `SILVER_MAX_AGE_MINUTES` | `150` | Fraîcheur des dernières données utilisables |
| `SILVER_STALLED_MINUTES` | `20` | Âge maximal du signal du coordinateur |
| `SILVER_PENDING_AGE_MINUTES` | `60` | Seuil d'ancienneté du backlog |
| `SILVER_EXPECTED_FORECAST_DAYS` | `3` | Couverture attendue de la dernière collecte |

Les deux paramètres de calendrier doivent être définis pour indexer les nouveaux
bindings ; Terraform les fournit. Les SDKs utilisent la connexion de stockage
existante ou la chaîne de credentials Azure de S3. Les valeurs de connexion ne
sont jamais passées en arguments CLI ni inscrites dans les rapports.

Changer source, racine, version ou borne de départ avec un checkpoint existant
produit une erreur explicite ; ce changement ne réinitialise pas silencieusement
les travaux. Pour une nouvelle transformation, créer une nouvelle version/racine.
Pour un changement de périmètre de la même version, désactiver le timer, examiner
et sauvegarder l'état, puis définir un plan de migration contrôlé. Ne pas effacer
le checkpoint comme procédure de reprise ordinaire.

## Supervision et notifications

Chaque lot émet `SILVER_RUN`, avec publications, rejeux, erreurs, travaux restants,
durée et pic RSS du processus Python. Ce pic inclut les autres fonctions du même
worker ; ce n'est pas une allocation mémoire isolée de Silver.

La sonde lit le checkpoint et émet `SILVER_HEALTH`. Elle conserve aussi un snapshot
diagnostique dans `_control/runtime/health.json`, sans modifier les tables ni le
coordinateur. Toute lecture de ce snapshot doit contrôler `checked_at` : un ancien
fichier n'est pas une preuve que la sonde continue à tourner.

La fraîcheur dépend de `collected_at_utc` des dernières lignes utilisables,
jamais de la date de rejeu ni du `last_modified` du blob. La qualité suit la plus
récente collecte : un ancien rejet de Londres dans le bootstrap ne remplace pas
le statut d'une collecte plus récente correcte. Une réponse partielle, vide,
rejetée ou avec moins de trois jours est signalée. Les journées DST conservent
leurs 23 ou 25 heures ; aucune règle de 72 heures glissantes n'est imposée.

Terraform ajoute cinq règles : fraîcheur/coordinateur, erreurs techniques,
backlog, qualité et absence de sonde depuis 45 minutes. Le filtre `cloud_RoleName`
limite leurs signaux à la Function App du projet. Les émissions d'un environnement
de validation ne peuvent pas masquer l'absence de signaux du runtime principal.

`SILVER_ALERT_ACTION_GROUP_IDS` (variable GitHub contenant une liste JSON) raccorde
des groupes d'actions existants. Sans groupe, les alertes restent visibles dans
Azure ; aucun email n'est promis. `SILVER_MONITORING_ENABLED=false` empêche la
création des cinq règles, sans désactiver les timers ni leurs traces.

## Vérifier le bootstrap et reprendre

1. Vérifier les six fonctions indexées et les deux nouveaux calendriers.
2. Lire `coordinator.json` et vérifier `history.passes >= 1`, absence d'erreurs
   récurrentes et drainage de `pending`. Le producteur peut continuer à collecter.
3. Figer une liste des identités Bronze du périmètre et la rapprocher des
   publications complètes. Une collecte `failed` donne une publication `empty` ;
   un JSON inexploitable peut donner `rejected`. Une publication ne signifie donc
   pas nécessairement qu'il existe des lignes métier pour cette ingestion.
4. Utiliser le plan de lecture `published_read_plan` : versions Delta figées et
   filtre sur les identités publiées. Rapprocher les lignes et les rejets ; ne pas
   lire les Parquet par glob et ne pas attendre une transaction commune aux tables.
5. Vérifier deux collectes d'heures successives et un rejeu sans changement de
   versions ni doublons. Les révisions de prévision restent distinctes par collecte.

En cas d'erreur technique, corriger l'accès ou la cause, puis laisser le prochain
timer reprendre. Les ingestions en attente conservent leur chemin, nombre de
tentatives et prochaine échéance. Un redémarrage de la Function App ne perd pas
ces travaux. Le CLI `scripts/silver_backfill.py` reste disponible, en preview par
défaut, pour un périmètre explicite ; le lease S3 arbitre sa concurrence avec le
timer. Ne jamais vider Bronze, la quarantaine ou les tables pour débloquer un lot.

## Dépendances et validation avant fusion

Python 3.11 Linux, Flex Consumption 2 Gio. Les pins `deltalake==1.2.1` et
`pyarrow==21.0.0` ont une seule définition dans `functions/requirements-silver.txt`.
Le fichier root de développement et le paquet Functions la référencent ; le zip
contient ainsi toutes les définitions nécessaires au build distant.

Le workflow **Validate S4 on isolated Azure Functions** est lançable manuellement.
Il crée une Function App et un plan Flex temporaires ; les fonctions Bronze y
sont désactivées. Il inventorie le vrai Bronze (jusqu’à 100 000 objets) puis valide le bootstrap
de toutes les archives d’une fenêtre déclarée de 48 heures maximum, jusqu’à
environ 80 ingestions, vers une racine isolée. La taille et la borne de ce
périmètre sont inscrites dans le rapport ; il ne prétend pas charger tout le
Bronze métier avant fusion. Le parcours historique sans borne de départ est
aussi testé sur le conteneur isolé et par les régressions multi-semaines. Il
puis copie sans modification deux archives réelles d'heures consécutives dans un
conteneur temporaire pour tester l'arrivée progressive des fichiers.

L'essai provoque un arrêt entre les commits, conserve son checkpoint, laisse le
vrai timer terminer, bloque le verrou S3, vérifie l'attente puis la reprise et un
rejeu automatique sans doublon. La sonde doit produire son snapshot. Les cinq
requêtes Monitor sont aussi acceptées par l'API Azure sous forme de règles
temporaires désactivées. L'app, son plan, ces règles, les conteneurs et les préfixes
possédés sont supprimés, avec une étape de nettoyage de secours indépendante.
Les métadonnées internes de l'hôte Functions dans son stockage partagé restent
gérées par la plateforme ; elles n'alimentent aucune table métier.

Le rapport `silver-s4-report.json` et le manifeste non secret des ressources sont
conservés dans l'artefact `silver-s4-runtime-evidence`. La branche principale et sa
Function App ne sont ni déployées ni modifiées par cet essai. La PR reste ouverte
jusqu'à sa fusion explicitement demandée.
