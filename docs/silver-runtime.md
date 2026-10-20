# S4 : batch Silver quotidien J+1

Après fusion et déploiement, `weather_silver_processor` ouvre un batch **chaque
jour à 02 h UTC**. Le 24 à 02 h, il demande les archives **collectées le 23**,
avec une borne exclusive au 24 à 00 h UTC. Une prévision collectée le 23 pour
le 25 reste incluse : on filtre la collecte, jamais la date météo visée.
Les collectes Bronze restent horaires et indépendantes. Aucun appel WeatherAPI
ni suppression d'archive Bronze n'est effectué par Silver.

## Stockage : deux dossiers métier et un dossier technique

Le stockage est le compte **ADLS Gen2 existant**, conteneur **`datalake`**.
Silver est un ensemble de fichiers dans ce compte, pas une nouvelle ressource
Azure ni une base SQL provisionnée séparément.

```text
datalake/silver/weatherapi/schema=v1/transform=1.0.0/
  observation_snapshots/             # table Delta des observations
    *.parquet                       # fichiers de données (noms gérés par Delta)
    _delta_log/                     # transactions de cette table
  forecast_hourly_snapshots/         # table Delta des prévisions horaires
    *.parquet
    _delta_log/
  _control/                         # suivi technique commun, pas une 3e table BI
    writer.lock
    publications/<ingestion_id>.json
    quarantine/<ingestion_id>.jsonl
    runtime/coordinator.json
    runtime/health.json
```

Les deux noms physiques, issus des contrats S1–S3, désignent respectivement les
**observations** et les **prévisions**. Chaque dossier est une table Delta unique,
commune à toutes les journées ; le passage au batch ne crée pas un dossier/table
par jour. Au volume actuel, les tables restent sans partition physique. Les
colonnes `ingestion_date` et `collected_at_utc` permettent de sélectionner les
journées. Toutes les versions collectées restent conservées, sans agrégation
quotidienne ni remplacement par la dernière prévision du jour.

`_control` contient des verrous, des bilans de publication, les diagnostics de
rejet et l'avancement des reprises. Les publications ne deviennent complètes
qu'après les deux commits Delta et la quarantaine. Gold utilise ce journal et
les versions indiquées pour exclure une ingestion interrompue entre les tables.
Ne pas effacer `_control` pour relancer un batch. Ne pas lire les Parquet par glob.
Détails : [stockage et lecture Gold](silver-storage.md).

## Une ouverture quotidienne, plusieurs lots si nécessaire

Le timer quotidien envoie un petit message à la file dédiée **`silver-runtime`**.
`silver_batch_worker` traite au plus 20 ingestions et respecte un budget de
240 secondes entre unités de travail. S'il reste des fichiers à découvrir,
traiter ou reprendre, il envoie la continuation avant d'acquitter le message.
Une journée de 24 collectes ne s'arrête donc pas au vingtième fichier. La suite
ne dépend ni du timer du lendemain ni d'un traitement météo toutes les cinq
minutes. Quand le batch est fini, aucune continuation n'est émise.

Les messages portent une borne de collecte fixe, au format UTC minuit. Un batch
qui traverse minuit ne commence pas à traiter le jour courant ; une nouvelle
ouverture quotidienne doit étendre sa borne. Les demandes dupliquées ne
réinitialisent ni les curseurs ni les publications. Le SDK Queue émet du JSON
non encodé, compatible avec `host.json` (`messageEncoding=none`). Terraform crée
la file ; aucun conteneur ou queue métier n'est créé implicitement par le worker.

Le checkpoint lui-même est protégé par un lease Azure renouvelé ; chaque
remplacement porte ce lease. Les candidats découverts et leur curseur sont
sauvegardés ensemble **avant** toute écriture Delta. Le lease S3 protège
séparément les écritures dans les deux tables. Les contrôles restent bornés et
paginés, y compris la recherche initiale à travers plus de mille anciens objets.

## Historique, jours manqués et arrivées tardives

Deux parcours partagent les lots et disposent chacun de la moitié de la
capacité des travaux en attente :

- **Quotidien** : J-1, avec relecture de J-2 par défaut pour les arrivées tardives.
  Après une interruption de plusieurs jours, tous les jours manqués depuis la
  précédente borne sont couverts, avec leur fenêtre de rattrapage.
- **Historique** : premier chargement de toutes les archives canoniques retenues
  avant la borne du batch, par fenêtres de sept jours. Ce bootstrap peut utiliser
  de nombreuses continuations. Une fois terminé, il reste au repos ; une nouvelle
  réconciliation devient éligible sept jours plus tard et démarre lors d'un batch
  quotidien. Elle redécouvre aussi les archives anciennes arrivées tardivement.

`SILVER_START_AT` restreint explicitement le périmètre si nécessaire. Sans ce
paramètre, aucune date initiale n'est devinée. Le premier chargement n'est pas
relancé chaque jour. La réconciliation reconnaît les ingestions déjà publiées :
aucun `MERGE`, doublon ou nouveau commit Delta pour ces ingestions. Les lectures
Bronze et du journal restent nécessaires pour vérifier leur identité et leur hash.

Une découverte terminée ne signifie pas que le traitement a réussi : la borne
quotidienne complète et le compteur de passages historiques ne sont validés
qu'après résolution des travaux correspondants. Les erreurs techniques restent
en attente sans limite de tentatives métier, avec un délai de 30 secondes doublé
jusqu'à une heure. La continuation est différée en conséquence, sans boucle vide.
Une contention de verrou est un résultat `busy`, pas un rejet métier.

Si une invocation s'arrête après publication mais avant checkpoint, la reprise
reconnaît cette publication sans nouvel écrit Delta. Un échec d'envoi de la suite
fait échouer le message courant : Azure Queue le retente selon `host.json`, puis
le place dans `silver-runtime-poison` après cinq échecs d'invocation. Le prochain
batch quotidien reprend le checkpoint conservé ; les alertes signalent entre-temps
les erreurs, le retard ou l'absence d'avancement. Corriger la cause avant toute
reprise manuelle ; ne pas vider la file poison comme procédure de réparation.

## Configuration

| Paramètre | Défaut | Rôle |
| --- | --- | --- |
| `SILVER_ENABLED` | `true` | Interrupteur du timer, du worker et de la sonde |
| `SILVER_TIMER_SCHEDULE` | `0 0 2 * * *` dans Terraform | Ouverture quotidienne à 02 h UTC |
| `SILVER_BATCH_HOUR_UTC` | `2` | Même heure, utilisée pour valider les bornes et les échéances |
| `SILVER_QUEUE_NAME` | `silver-runtime` | File dédiée aux continuations, distincte de Bronze |
| `SILVER_HEALTH_SCHEDULE` | `0 */15 * * * *` | Sonde indépendante toutes les 15 minutes |
| `SILVER_ROOT` | `silver/weatherapi/schema=v1/transform=1.0.0` | Tables et contrôles de cette version |
| `SILVER_START_AT` | Première archive retenue | Restriction UTC optionnelle de l'historique |
| `SILVER_REPLAY_DAYS` | `2` | J-1 et J-2 relus par le parcours quotidien |
| `SILVER_RECONCILE_DAYS` | `7` | Délai entre réconciliations historiques complètes |
| `SILVER_COMPLETION_GRACE_MINUTES` | `120` | Batch attendu terminé à 04 h UTC par défaut |
| `SILVER_BATCH_SIZE` | `20` | Ingestions tentées par invocation, maximum 100 |
| `SILVER_DISCOVERY_LIMIT` | `40` | Objets inspectés par page, maximum 100 |
| `SILVER_DISCOVERY_PAGES` | `48` | Appels de pages par parcours, maximum 168 |
| `SILVER_PENDING_LIMIT` | `500` | Travaux persistants, maximum 1 000 |
| `SILVER_BUDGET_SECONDS` | `240` | Budget entre unités de travail, maximum 480 |
| `SILVER_MAX_AGE_MINUTES` | `150` | Tolérance entre la dernière collecte utilisable et la fin du jour attendu |
| `SILVER_STALLED_MINUTES` | `20` | Retard toléré après la prochaine continuation attendue |
| `SILVER_PENDING_AGE_MINUTES` | `180` | Seuil d'ancienneté des travaux en attente |
| `SILVER_EXPECTED_FORECAST_DAYS` | `3` | Couverture attendue de la collecte la plus récente |

La variable GitHub `SILVER_BATCH_HOUR_UTC` alimente Terraform, qui règle ensemble
l'expression du timer et l'heure métier ; modifier seulement le cron créerait une
incohérence. `SILVER_ENABLED` est également conservé par Terraform. Les secrets
restent dans les réglages existants, jamais dans les messages ou rapports.

Le checkpoint de ce batch est au schéma **2**. Un ancien checkpoint S4 de
l'orchestrateur toutes les cinq minutes est refusé explicitement. Au moment de
cette adaptation, la racine Silver définitive n'a pas été initialisée et la PR
n'est pas fusionnée : aucune migration de données métier n'est requise. Pour un
checkpoint existant hors tests, prévoir une migration contrôlée avant changement,
sans suppression ni réinitialisation automatique de l'avancement.

## Supervision

Chaque lot émet `SILVER_RUN` avec sa borne, les publications, les rejeux, le
nombre de travaux restants, les erreurs, la suite attendue, la durée et le pic RSS.
`silver_health_monitor` émet `SILVER_HEALTH` et sauvegarde un snapshot horodaté.
La sonde est en lecture seule vis-à-vis des tables et du coordinateur.

La fraîcheur suit désormais **le dernier jour qui devait être livré**, avec une
grâce de deux heures après l'ouverture quotidienne. Entre deux batchs terminés,
l'absence d'invocations n'est pas une panne. Une continuation en retard, un jour
non terminé après son échéance ou l'absence de données utilisables en fin de
journée sont signalés. Rejouer une vieille archive ne la rend pas récente : la
date de collecte source est conservée. La qualité suit la collecte la plus récente,
y compris les rejets et les horizons incomplets ; un ancien rejet historique ne
remplace pas une collecte récente correcte.

Les cinq règles Monitor surveillent fraîcheur/échéance, erreurs, backlog, qualité
et absence de sonde depuis 45 minutes. Le filtre `cloud_RoleName` les limite à
l'application principale. Sans `SILVER_ALERT_ACTION_GROUP_IDS`, les alertes restent
visibles dans Azure, sans notification externe. Les ZIP de déploiement, le stockage
`azure-webjobs-*` et les artefacts GitHub de preuve sont distincts des tables métier.

## Mise en service et vérification

1. Vérifier la création de `silver-runtime`, les **sept fonctions** indexées
   (quatre Bronze, ouverture quotidienne, worker et sonde Silver), et le timer à 02 h UTC.
2. Après l'ouverture, suivre `daily.completed_end`, `history.passes >= 1`, `pending`
   et `next_wake_at` dans `_control/runtime/coordinator.json`. La fin d'énumération
   seule ne prouve pas la livraison.
3. Contrôler les publications complètes et les versions des deux tables avant Gold.
4. Vérifier la fraîcheur de `_control/runtime/health.json` et le raccordement des notifications.
5. Pour une panne, corriger la cause puis laisser/reprendre la demande via la file
   dédiée ; conserver tous les checkpoints, rejets et publications.

Le helper `scripts/validate_silver_runtime_azure.py` utilise une app, un plan, des
files et des racines ADLS temporaires possédés. Le timer y est accéléré et l'heure
de batch y vaut 00 h, uniquement pour tester sans attendre une journée ; il ne
modifie pas le calendrier de production. Il contrôle les continuations réelles,
les jours fermés, une interruption entre tables, deux batchs consécutifs de part
et d'autre de minuit, la contention, le rejeu et les cinq règles Monitor. Il
nettoie ses ressources dans un `finally` et dans une étape de secours du workflow.
Ces essais n'initialisent pas les tables définitives et ne déploient pas `main`.
