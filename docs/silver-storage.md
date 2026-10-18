# Silver Delta : stockage, publication et backfill

Ce lot S3 dépend des contrats et bibliothèques S1 et du transformateur S2.
Il ne déploie aucune ressource et ne modifie ni Bronze ni l’API météo.
Les 56 tests S3 exécutés localement utilisent de **vraies transactions Delta**,
y compris sept scénarios avec le transformateur S2 réel (dont les deux fixtures
dérivées de Bronze S1) ; l’authentification,
les leases et collisions sur ADLS restent des contrôles d’acceptation Azure S4.

## Deux tables communes aux ingestions

```text
datalake/silver/weatherapi/schema=v1/transform=1.0.0/
  observation_snapshots/_delta_log/...
  forecast_hourly_snapshots/_delta_log/...
  _control/writer.lock
  _control/publications/<ingestion_id>.json
  _control/quarantine/<ingestion_id>.jsonl
```

Chaque racine métier est une table Delta unique, initialement sans partition
physique. Les schémas sont explicites, même pour une première ingestion vide.
Les clés incluent `ingestion_id` : une nouvelle collecte conserve ses observations
et prévisions, y compris lorsqu’elle vise une heure déjà présente dans une autre
collecte. Le format n’applique aucune contrainte d’unicité à notre place.

L’écrivain vérifie le schéma, les clés et l’ensemble des valeurs déjà présentes
pour cette ingestion, dont l’empreinte des octets Bronze. Il refuse une différence
ou une clé dupliquée et insère seulement les lignes manquantes par `MERGE`.
Il n’effectue ni mise à jour de snapshot, ni évolution implicite de schéma,
ni `VACUUM`, ni réécriture des fichiers Parquet en dehors de Delta.

## Publication et reprise

`SilverRunner.process_ingestion(source_blob)` renvoie `ProcessResult` :
`outcome=published|already_processed|busy`, `ingestion_id`, `publication`.
Les erreurs de stockage ou de conflit remontent à l’appelant. `busy` demande une
reprise. `status=success|partial|rejected|empty` décrit le résultat métier publié.
Un JSON invalide peut être publié `rejected` avec deux tables vides ; le chemin
Bronze canonique fournit alors l’identité du diagnostic.

1. Lire Bronze, calculer son SHA-256 et vérifier la publication éventuelle.
2. Transformer un seul blob, sans appel API ni écriture.
3. Acquérir le lease commun à cette version puis revérifier la publication.
4. Vérifier et fusionner les observations, puis les prévisions horaires.
5. Écrire la quarantaine immuable et publier le bilan après les deux transactions.

Le lease Azure dure 60 secondes, se renouvelle en tâche de fond toutes les
15 secondes et se renouvelle aussi à chaque point de contrôle. Toute erreur de
renouvellement le marque définitivement perdu ; le code cesse les étapes
suivantes. Un autre écrivain renvoie `busy` lorsqu’un lease est détenu.

**Le lease est coopératif et ne clôture pas un commit Delta déjà en vol.**
Un administrateur ou un autre programme qui écrit hors protocole peut violer ce
contrat. Les `MERGE` travaillent depuis le snapshot Delta dont les valeurs ont été
vérifiées ; l’isolation `Serializable` et les collisions optimistes complètent le
lease. Les retries de commit automatiques sont désactivés : après un conflit, on
reprend depuis Bronze avec nouveau lease et nouveau snapshot, puis on compare à
nouveau les valeurs. Le test de deux `MERGE` sur des snapshots concurrents vérifie
le comportement effectif du moteur **local**, sans prétendre prouver son
comportement ADLS avant l’essai Azure. Aucune promesse de fencing matériel n’est
faite entre le dernier renouvellement et une requête de stockage en vol.

Une panne entre les tables laisse des lignes sans publication : la reprise
retrouve la première table, valide les valeurs, complète la seconde et publie.
Une publication déjà compatible est un no-op. Un hash Bronze différent sous le
même identifiant est une erreur visible, même si une publication existe.
Un changement de logique exige une nouvelle `TRANSFORM_VERSION` et une nouvelle
racine ; ne pas modifier en place les snapshots d’une version publiée.

## Contrat de lecture Gold

Delta fournit une transaction **par table**, sans transaction commune aux deux
tables. Gold doit figer sa liste de publications complètes, appeler
`published_read_plan(publications)`, ouvrir chaque table à la version renvoyée et
filtrer les lignes sur les `ingestion_ids` de ce plan. Le maximum des versions
publiées contient les snapshots antérieurs car les écritures sont insert-only.
Les publications arrivant ensuite appartiennent au prochain lot Gold.

Un lecteur qui ignore le journal peut voir une ingestion incomplète. Il ne faut
pas lire les fichiers Parquet par glob. Les versions Delta sont des positions de
stockage, distinctes de la version fonctionnelle de transformation.

## Découverte bornée et travaux en attente

`discover_bronze(container, start, end, limit=200, max_pages=48, cursor=None)`
inspecte au plus 200 blobs et fait au plus 48 appels de pages par invocation.
Elle parcourt les partitions UTC de `[start,end)`, filtre les identifiants de
collecte, et renvoie `DiscoveryPage(blobs, inspected, continuation)`.
Les bornes sont configurables ; une limite atteinte conserve un curseur lié à la
fenêtre d’origine. Les partitions vides sont elles aussi bornées.

Le curseur indique le progrès d’énumération, **pas** la réussite de traitement.
S4 doit sauvegarder les candidats avant de faire avancer son curseur et garder
les erreurs techniques en attente jusqu’à publication. Le runner et cette
fonction de découverte sont les interfaces communes au timer et au backfill.

## CLI de backfill

Installer les dépendances S1, configurer la connexion existante via
`STORAGE_CONNECTION_STRING` ou `STORAGE_ACCOUNT_NAME` et la chaîne de credentials
Azure par défaut. Aucune clé ni URL signée n’est acceptée dans les arguments.
Le compte doit pouvoir lire Bronze et lire/écrire/committer dans le conteneur
`datalake` ; les contrôles ont aussi besoin de créer, renouveler et relâcher un
lease Blob. Les deux SDKs doivent utiliser le même compte public Azure.

```bash
# Lecture et aperçu uniquement, aucun lease/commit/publication.
python scripts/silver_backfill.py --from 2026-10-01T00:00:00Z --to 2026-10-02T00:00:00Z

# Traiter un lot borné puis relancer la même commande avec le même fichier.
python scripts/silver_backfill.py --from 2026-10-01T00:00:00Z --to 2026-10-02T00:00:00Z --execute --state-file /tmp/silver-backfill.json
```

L’exécution exige un fichier d’état durable et dédié à cette fenêtre/version.
Le lot découvert et sa continuation sont sauvegardés **avant** toute écriture ;
chaque publication réussie retire un candidat de la liste. Les erreurs et `busy`
y restent. Un arrêt après commit mais avant la sauvegarde provoque un rejeu sûr.
Utiliser un seul opérateur par fichier d’état, le conserver jusqu’à achèvement et
ne pas le réinitialiser pour ignorer une erreur. `--limit` accepte 1 à 100.
Le preview ne modifie même pas le fichier d’état. Les publications compatibles
sont indiquées `already_processed` après vérification du hash Bronze.

Codes retour : `0` = terminé, `2` = continuation ou candidats restant en aperçu,
`1` = erreur ou contention. Les messages d’erreur ne donnent que le nom de leur
classe, sans contenu SDK ni secret. L’outil ne supprime aucun blob et n’appelle
jamais WeatherAPI.

## Vérifications Azure restantes

Sur un préfixe d’essai isolé : authentification Azure Blob + delta-rs sur le même
compte, création/lecture des deux schémas, `MERGE`, conflit de deux writers,
renouvellement/perte de lease et panne entre commits ; comparer publications,
versions et lignes par un lecteur Delta indépendant. Vérifier ensuite le paquet
Azure Functions Python 3.11, la mémoire et le chargement effectif des fonctions
Bronze/Silver. Les tests locaux ne valent pas déploiement ni acceptation Azure.

## Acceptation ADLS isolée préparée

`make_azure_runner(root=...)` accepte une racine relative `silver/...` validée
avant tout accès aux credentials. Les chemins absolus, traversal, URL/paramètres,
segments vides et encodages sont refusés. Les tables et objets de contrôle sont
routés ensemble sous cette racine ; la valeur par défaut reste la racine métier.

Le script d’acceptation remis au parent, `validate_delta_adls.py`, ne doit être
exécuté que depuis son workflow de validation. Il lit deux Bronze existants et
utilise un préfixe unique `silver/_validation/s1-s3-<uuid>/`, sans appel WeatherAPI
ni écriture Bronze/queue. Il couvre valeurs réelles, rejeu sans nouveau commit,
panne entre tables, filtre Gold, contention et perte du lease d’essai, et conflit
optimiste de deux snapshots Delta. Son `finally` nettoie uniquement les objets
sous le préfixe possédé et écrit un rapport JSON sans erreur SDK brute ni secret.

À ce stade, le script est préparé et les tests de garde sont exécutés localement ;
**aucun résultat Azure n’est revendiqué avant son exécution par le parent**.
