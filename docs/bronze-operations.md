# Exploitation et reprise de Bronze

Ce guide complète le contrat dans [bronze-ingestion.md](bronze-ingestion.md).
Il concerne la collecte horaire de Paris, Londres et Bruxelles. Les reprises
republient uniquement des références existantes : elles n'appellent jamais
WeatherAPI et ne recréent pas une donnée historique perdue.

## Installer et se connecter

Depuis la racine du dépôt, avec Python 3.11 et les dépendances du projet :

```sh
python -m venv .venv
.venv/bin/python -m pip install -r functions/requirements.txt
az login
export STORAGE_ACCOUNT_NAME='<nom-du-compte>'
.venv/bin/python scripts/bronze_recovery.py --help
```

Le CLI utilise `DefaultAzureCredential` (session Azure CLI, identité managée ou
identité de service configurée). L'identité doit avoir `Storage Blob Data Reader`
et `Storage Queue Data Contributor` sur le compte. La propagation RBAC peut
prendre quelques minutes. `STORAGE_CONNECTION_STRING`, si déjà injectée dans
l'environnement, prend la priorité ; ne pas la passer en argument ou la copier
dans un ticket. Aucune clé WeatherAPI n'est nécessaire.

Les valeurs par défaut sont `STAGING_CONTAINER=staging`, `BRONZE_CONTAINER=bronze`
et `QUEUE_NAME=ingestion-weatherapi`. Les changer dans l'environnement uniquement
si la Function utilise les mêmes paramètres. `QUEUE_NAME-poison` est la queue
poison correspondante. Le CLI n'affiche ni le corps des données, ni les URL/SAS,
ni les pop receipts, ni le texte d'une exception SDK.

## Examiner puis reprendre un staging sans message

```sh
# Lecture seule : jusqu'à 20 staging modifiés depuis au moins 30 minutes.
.venv/bin/python scripts/bronze_recovery.py scan-staging --older-than-minutes 30 --limit 20

# Lecture seule : valider précisément le candidat choisi.
.venv/bin/python scripts/bronze_recovery.py staging --ingestion-id '<ingestion_id>'

# Publication explicite dans la queue ; le staging reste au consommateur Bronze.
.venv/bin/python scripts/bronze_recovery.py staging --ingestion-id '<ingestion_id>' --execute
```

`scan-staging` trouve des **candidats** : un vieux blob peut déjà avoir un message
dans la queue normale ou un consommateur actif. Il ne prouve pas qu'il est
orphelin. La recherche est bornée à 1 000 blobs inspectés et 100 résultats ;
`scan_truncated` signifie qu'il faut examiner le conteneur Azure pour compléter.
Un message dupliqué est attendu et le consommateur idempotent s'en charge.

`preview` signifie « les données existent et une publication serait possible » ;
aucun envoi n'a eu lieu. `published` signifie « Azure Queue a confirmé l'envoi »,
pas « le traitement Bronze est terminé ». `already_bronze` ne publie rien et ne
nettoie aucun staging. Confirmer ensuite l'existence du chemin Bronze calculé
depuis `ingestion_timestamp`, puis le retrait du staging par le consommateur.
Si le staging a déjà disparu, cette commande ne peut pas déduire la partition
Bronze du seul identifiant : utiliser le message d'origine ou rechercher le
fichier dans Azure. La commande poison dispose, elle, du timestamp du message.

Le CLI reconstruit un message v1 depuis le staging, avec sa taille UTF-8 réelle.
Il contrôle son identifiant, son horodatage et ses compteurs. Une collecte brute
`failed` reste archivable ; sa remise en queue ne corrige pas les appels API.

## Examiner puis reprendre la poison queue

Après cinq traitements échoués, le runtime Functions place le message dans
`<QUEUE_NAME>-poison`. Vérifier d'abord la cause et déployer sa correction :
rejouer un message invalide sans corriger la cause le renverrait en poison.

```sh
# Lecture seule par peek : aucun message rendu invisible, aucun acquittement.
.venv/bin/python scripts/bronze_recovery.py poison --limit 5

# Après diagnostic/correction, traiter le premier message visible.
.venv/bin/python scripts/bronze_recovery.py poison --limit 1 --execute
```

Une exécution est bornée à 1–32 messages visibles (1 par défaut). Elle peut
recevoir d'autres messages que la prévisualisation si la queue a changé.
Chaque message reçu est invisible pendant 120 secondes ; les messages bloqués
ou en erreur restent présents et redeviennent visibles à l'expiration de ce délai.
L'exécution ne déplace pas automatiquement tous les messages de la queue.

| Résultat | Effet |
| --- | --- |
| `preview` | Staging récupérable, aucun envoi ni suppression |
| `published_acknowledged` | Message normal confirmé, puis poison acquitté |
| `already_bronze_acknowledged` | Bronze existe déjà ; seul le poison est acquitté |
| `published_unacknowledged` | Envoi confirmé, acquittement échoué ; poison conservé, doublon possible |
| `already_bronze_unacknowledged` | Bronze existe, acquittement échoué ; poison conservé |
| `blocked` | Message invalide, staging incohérent ou source absente ; aucune suppression |
| `error` | Erreur de lecture/publication ; aucune suppression du poison |
| `poison_queue_absent` | Queue pas encore créée par Functions ; rien à reprendre, aucune mutation |

Un timeout réseau peut laisser une publication incertaine. Le CLI **n'acquitte
jamais le poison sans confirmation d'envoi ou présence préalable du Bronze**.
Il ne garantit pas un envoi unique ; le consommateur assure le rejeu sans doublon
Bronze. Un pop receipt expiré empêche l'acquittement et conserve aussi le poison.
La queue n'est jamais purgée en bloc.

Le contrat existant est réutilisé, y compris les messages antérieurs à la v1.
Avant publication, l'identifiant et l'horodatage du staging doivent correspondre
au message (comparaison après normalisation UTC). Un message indiquant la mauvaise
heure est bloqué ; si le staging est sain, utiliser la commande `staging` avec
l'identifiant pour créer une référence correcte, vérifier Bronze, puis traiter
le poison fautif avec une procédure manuelle documentée. Le CLI ne réécrit pas
automatiquement les messages invalides et ne propose pas de suppression forcée.

Si staging et Bronze sont absents, le CLI retourne
`staging_missing_or_expired`. La rétention staging et la durée de vie des messages
normaux sont de sept jours. Le CLI ne prolonge pas le staging et ne peut pas
restaurer son contenu après expiration. Enregistrer l'incident, conserver la
référence et rechercher une sauvegarde éventuelle. Une nouvelle collecte météo
ne remplace pas la prévision historique d'origine.

Les sorties sont des objets JSON, un par résultat. Le code de sortie est 1 en
cas de blocage, erreur, acquittement échoué ou scan tronqué ; sinon 0. Une queue
vide ne produit aucune ligne et retourne 0.

## Sonde et alertes Azure

`bronze_health_monitor` tourne toutes les 15 minutes. Il lit seulement les
partitions horaires couvrant les 150 dernières minutes plus l'heure de marge
(quatre partitions), puis les petits JSON correspondants. Les limites sont de
100 blobs par partition et 100 contenus par exécution ; les dépasser produit une
erreur explicite. Ce choix convient à une collecte horaire et doit être revu si
le volume augmente. Aucun scan complet de l'historique Bronze n'est effectué.

La fraîcheur est calculée depuis **l'horodatage de collecte contenu dans Bronze**,
pas la dernière modification du fichier. Un rejeu ancien ne remet pas la fraîcheur
à zéro. Une collecte à zéro ville reçue ne la remet pas à zéro non plus. Une
collecte partielle la remet à jour mais est signalée séparément. Le dernier
horodatage peut être nul lorsque les partitions récentes ne contiennent aucune
collecte exploitable ; cela signifie `stale`, jamais un état sain implicite.
Une tolérance de 60 secondes couvre une collecte démarrée pendant les lectures
réseau de la sonde et un petit décalage d'horloge ; un timestamp situé plus loin
dans le futur est refusé. Une erreur de lecture, un JSON incohérent ou une queue inaccessible émet
`freshness=error` et fait échouer la sonde.

Une queue poison absente est normale avant le premier incident : la sonde indique
`poison_messages=0` et `poison_queue_exists=false`, et le CLI retourne
`poison_queue_absent` avec le code 0. Seul `ResourceNotFoundError` est interprété
ainsi ; une erreur d'authentification, d'autorisation ou de réseau reste une erreur.

Chaque exécution émet une trace `BRONZE_HEALTH {JSON}` avec la fraîcheur, le
dernier statut exploitable et le compteur approximatif de messages poison. Le
fichier `host.json` exclut les traces et exceptions du sampling pour préserver
ces signaux de faible volume ; les réglages de sampling/ingestion au niveau de
la ressource Application Insights doivent également préserver ces événements.

Terraform ajoute quatre alertes Azure Monitor sur Application Insights :

| Alerte | Déclenchement | Vérification |
| --- | --- | --- |
| Fraîcheur | Dernière sonde `stale` ou `error` : aucune donnée exploitable depuis plus de 150 min, ou sonde défaillante | Dernier événement santé et fichiers Bronze |
| Poison | Dernier compteur approximatif supérieur à zéro | CLI `poison` en preview et compteur Azure Queue |
| Échecs / partiel | Trace d'échec de collecte, traitement ou contrat, ou collecte partielle depuis 30 min | Logs et ingestion concernée |
| Sonde absente | Aucun événement `BRONZE_HEALTH` depuis 45 min | État Function App, timer et ingestion Application Insights |

Évaluation toutes les 15 minutes, fenêtre de requête d'une heure, résolution
automatique quand la condition disparaît. La latence d'ingestion des logs et le
compteur approximatif peuvent retarder une alerte. Une indisponibilité d'Azure
Monitor lui-même n'est pas couverte par ces alertes.

Les alertes sont activées par défaut (`bronze_monitoring_enabled=true`) et
facturables selon la tarification Azure Monitor. Pour les désactiver, définir
la variable GitHub `BRONZE_MONITORING_ENABLED` à `false` dans l'environnement
`dev` ou fournir la variable Terraform correspondante. Le timer continue à
émettre ses traces ; aucun destinataire n'est inventé.

**Sans action group, les alertes sont visibles dans Azure Monitor mais aucun
email ni autre notification externe n'est envoyé.** Pour raccorder un action
group existant, définir `BRONZE_ALERT_ACTION_GROUP_IDS` dans l'environnement
GitHub `dev` avec un tableau JSON d'identifiants Azure de ressources, par exemple
`["/subscriptions/<id>/resourceGroups/<rg>/providers/microsoft.insights/actionGroups/<nom>"]`.
Les workflows plan/apply transmettent ce tableau à
`bronze_alert_action_group_ids`. Ne pas y mettre une adresse email ; les
destinataires se gèrent dans l'action group Azure existant.

La queue poison reste gérée par le runtime Functions ; Terraform ne la crée pas
et n'exige aucun import de queue préexistante.

## Vérifier après déploiement

1. Vérifier le plan Terraform : quatre alertes attendues, aucune
   suppression de données. Vérifier ensuite le déploiement Functions.
2. Attendre un passage de la sonde (15 min maximum), puis interroger Application
   Insights avec la requête ci-dessous. Confirmer `freshness=fresh`, un timestamp
   récent, le nombre de messages poison attendu et l'absence d'erreur de sonde.
3. Vérifier les quatre règles dans Azure Monitor et leurs action groups.
   Au premier déploiement, l'alerte sonde absente peut s'activer avant l'arrivée
   de la première trace ; elle se résout ensuite automatiquement.
4. Vérifier le CLI sur les ressources réelles en **preview**. Pour tester une
   reprise avec mutation, utiliser une ingestion de test identifiée, confirmer
   son Bronze après consommation et conserver les preuves dans la PR/l'incident.
   Ne pas injecter un message invalide dans la queue de production pour tester
   une notification. Les scénarios de panne sont couverts localement par doublures.

```kusto
traces
| where timestamp > ago(2h)
| where message startswith "BRONZE_HEALTH "
| extend health = parse_json(substring(message, 14))
| project timestamp, freshness=tostring(health.freshness),
    latest_ingestion_at=todatetime(health.latest_ingestion_at),
    latest_status=tostring(health.latest_status),
    poison_messages=tolong(health.poison_messages),
    poison_queue_exists=tobool(health.poison_queue_exists),
    error_type=tostring(health.error_type)
| order by timestamp desc
```

Les tests locaux utilisent les SDK réels avec appels Azure simulés. Ils couvrent
les confirmations d'envoi/acquittement, la source expirée, les horodatages UTC,
les doublons, les erreurs de sonde et la fraîcheur métier. Ils ne remplacent pas
la validation Azure après fusion/déploiement.
