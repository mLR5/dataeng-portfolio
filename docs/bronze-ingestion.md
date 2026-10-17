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
Les logs de retry n'incluent pas l'URL de la requête, qui contient la clé API.

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
sur une Function App Azure. L'idempotence du consommateur, le contrat des messages
et les procédures de reprise appartiennent aux lots Bronze suivants.
