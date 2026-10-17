# Collecte Bronze : exécution et validation

Le pipeline collecte Paris, Londres et Bruxelles puis archive chaque résultat
dans staging et publie sa référence en queue, y compris pour une collecte
partielle ou entièrement échouée. Cela conserve la trace brute de l'exécution.
Un succès de collecte ne signifie pas encore que le consommateur a écrit en Bronze.

## Configuration obligatoire

Avant tout appel externe, l'ingestion valide `WEATHERAPI_KEY`,
`STORAGE_CONNECTION_STRING`, `QUEUE_NAME`, `STAGING_CONTAINER` et
`BRONZE_CONTAINER`. Les trois derniers ont les valeurs par défaut du projet.
La liste `CITIES` doit être non vide. Une référence Key Vault non résolue est
refusée. Les valeurs des secrets ne sont pas incluses dans les erreurs de validation.
Les paramètres sont lus dans l'environnement au chargement du module ; redémarrer
le processus après un changement de configuration.

## Statuts

| Statut | Signification | HTTP manuel | Timer |
| --- | --- | --- | --- |
| `success` | Toutes les villes sont récupérées | 200 | Information |
| `partial` | Au moins une ville est récupérée, au moins une échoue | 200, corps `partial` | Avertissement |
| `failed` | Aucune ville n'est récupérée | 503, corps `failed` | Exception après archivage et publication en queue |

Une erreur de configuration ou de stockage retourne un HTTP 500 avec
`status=failed` et échoue aussi côté timer. Le corps HTTP doit être consulté pour
identifier un résultat partiel ; le code 200 ne garantit pas la complétude.

## Identifiants et partitions

Les nouveaux identifiants suivent `YYYYMMDD_HHMMSS_<uuid4>`. Le suffixe unique
évite les collisions entre collectes simultanées. `ingestion_timestamp` indique
explicitement le fuseau UTC. Les partitions Bronze `YYYY/MM/DD/HH` sont calculées
depuis cet horodatage, indépendamment de l'identifiant. Les anciens horodatages
sans fuseau sont interprétés comme UTC, conformément à leur génération initiale.

## Retries HTTP

Trois tentatives au maximum pour les timeouts, erreurs de connexion et codes
429, 500, 502, 503 et 504. Les autres erreurs HTTP ne sont pas retentées.
L'attente exponentielle est de 1 puis 2 secondes ; un `Retry-After` valide
(secondes ou date HTTP) impose une attente au moins égale au délai annoncé.
Un `Retry-After` supérieur à 30 secondes n'est pas attendu : la ville est
immédiatement marquée en échec, pour ne pas bloquer la Function jusqu'à son timeout.
Les logs de retry n'incluent pas l'URL de la requête, qui contient la clé API.

## Contrat des messages (version 1)

La collecte publie un message Claim Check par exécution dans la queue
configurée par `QUEUE_NAME`. Terraform définit ce nom une seule fois, dans le
module storage, et le transmet à la Function App ; le binding du consommateur
le lit via `%QUEUE_NAME%`.

| Champ | Description |
| --- | --- |
| `type` | Toujours `claim_check` |
| `schema_version` | `1` |
| `ingestion_id` | `YYYYMMDD_HHMMSS_<uuid4>` (ou `YYYYMMDD_HHMMSS` pour les anciennes collectes) |
| `ingestion_timestamp` | Horodatage ISO UTC, source de la partition Bronze |
| `status` | `success`, `partial` ou `failed` |
| `blob_container`, `blob_name` | Toujours `staging` et `raw-ingestion/<ingestion_id>.json` |
| `blob_url` | URL du blob staging, informative |
| `payload_size_bytes` | Taille exacte du blob staging écrit |
| `cities_count`, `summary` | Nombre de villes et décompte succès/échecs |

Le consommateur accepte aussi les messages publiés avant la version 1 (sans
`schema_version`, taille éventuellement nommée `size_bytes`). Un message
illisible, d'une autre version ou pointant ailleurs que le blob staging attendu
est refusé sans aucun accès au stockage ; après les tentatives prévues, il part
dans la poison queue. Cette vérification empêche un message forgé de faire lire
ou supprimer un autre blob.

## Traitement Bronze rejouable

Le chemin Bronze est déterministe (partition horaire + `ingestion_id`). Le
consommateur peut donc recevoir plusieurs fois le même message sans doublon :

1. Si le blob Bronze existe déjà, il n'est ni relu ni réécrit.
2. Sinon le blob staging est téléchargé et son `ingestion_id` comparé à celui
   du message, puis écrit en Bronze sans écrasement. Si un traitement parallèle
   l'a écrit entre-temps, la version existante est conservée.
3. Le staging n'est supprimé qu'une fois Bronze présent. Un staging déjà
   supprimé n'est pas une erreur. Un échec de suppression est journalisé sans
   faire échouer le message : la lifecycle policy purge le staging après 7 jours.

| Situation | Résultat |
| --- | --- |
| Première livraison | Bronze écrit, staging supprimé |
| Message rejoué après traitement complet | Aucune écriture |
| Arrêt entre l'écriture Bronze et le nettoyage | Seul le staging est supprimé |
| Staging et Bronze absents | Échec, le message est retenté puis part en poison queue |
| Staging d'une autre ingestion | Échec sans écriture ni suppression |

## Tests locaux et CI

Python 3.11 correspond au runtime Functions déclaré dans Terraform.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Les tests utilisent les dépendances réelles (requests, tenacity, SDK Azure) et
remplacent les appels HTTP et stockage ainsi que les attentes de retry par des
doublures. Aucun compte Azure, secret, appel WeatherAPI ou déploiement n'est requis.
Le workflow `Tests ingestion` exécute les tests sur les PR et les pushes sur main.
Les dépendances de test sont séparées du fichier déployé avec les Functions.

Cette suite vérifie la logique Python ; elle ne remplace pas un test d'intégration
sur une Function App Azure. La remise en queue des collectes échouées, la reprise
de la poison queue et les alertes appartiennent au lot Bronze suivant.
