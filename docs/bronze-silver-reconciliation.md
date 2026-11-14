# Réconciliation des données Bronze → Silver

Le contrôle `silver_check` vérifie les archives effectivement présentes en
Bronze contre les quatre tables Silver : observations, prévisions, quarantaine
et publications. Il est en lecture seule et échoue sur un écart inexpliqué.
Il ne déclenche ni collecte WeatherAPI, ni publication, ni réparation.

## Ce qui est vérifié

1. Chaque archive Bronze dans la période a exactement une publication complète.
   Les identifiants présents seulement en Silver sont signalés également, ainsi
   que les chemins d'archives non canoniques et les identités Bronze en double.
2. Un recomptage des sections `current` et des éléments réellement présents dans
   les listes `hour` précède la validation géographique. Pour chaque table :

   ```text
   créneaux Bronze = exclusions de ville/enveloppe + lignes candidates
   lignes candidates = lignes valides + lignes rejetées + doublons supprimés
   ```

   Les totaux par ville doivent rejoindre les totaux globaux. Les conflits sont
   inclus dans les lignes rejetées et ont un diagnostic pour chaque ligne.
3. Toutes les colonnes Silver doivent correspondre aux valeurs recalculées
   avec le contrat versionné, y compris les clés, dates, nulls, métriques,
   versions, chemins et SHA-256 des octets Bronze. La comparaison est un
   **multiensemble**, pas seulement un volume ou un ensemble de clés : perdre
   une ligne et en dupliquer une autre fait échouer le contrôle.
4. Les rejets de quarantaine doivent correspondre exactement aux diagnostics
   attendus. Statut, bilan, compteurs, identité et empreintes de publication
   doivent être cohérents. Les références Delta futures/négatives sont refusées.

Les versions Delta sont figées au début, publications en premier, puis tables
métier et quarantaine. Les données détaillées sont lues par lots de 100 archives
par défaut ; le rapport expose des compteurs, positions et codes, sans recopier
les valeurs météo brutes. Le listing historique et les rapports par archive
restent en mémoire sur le driver. Un audit complet entraîne des lectures ADLS
et du calcul serverless ; il n'est pas ajouté au job quotidien.

## Interpréter les différences

Un vert signifie **conforme aux règles actuelles**, pas « toutes les collectes
sont bonnes ». Le rapport garde les statuts `partial`, `rejected`, `empty`, les
échecs de collecte, les motifs de rejet, la couverture et les champs ignorés.
Un JSON impossible à parser est explicitement non dénombrable
(`bronze_countable=false`), avec rejet `invalid_json` ; ce n'est pas une collecte
sans perte certifiée.

Les pertes expliquées par le contrat incluent : métriques invalides, ville
inconnue ou mal localisée, doublons identiques, conflits de clé, enveloppe
invalide et échec de collecte. Elles restent visibles pour juger leur impact.
Une disparition de ligne valide, une valeur altérée, un rejet non persisté,
une publication manquante ou un dédoublonnage entre ingestions est une anomalie.

Cas réel versionné : chaque extrait des 17 et 18 octobre 2026 contient trois
villes, trois observations candidates et dix-huit heures de prévision. Londres
est située en Argentine : son rejet `location_mismatch` explique une observation
et six heures exclues. Il reste **deux observations et douze prévisions par
archive**, sans perte inexpliquée. Les extraits ne contiennent que deux heures
par jour ; ne pas les présenter comme les archives originales complètes.

Les heures/jours absents de la réponse fournisseur n'ont jamais été reçus en
Bronze et ne sont pas des pertes Bronze → Silver. La couverture reste visible,
sans imposer 24 heures aux journées de changement d'heure. Les champs hors
contrat Silver restent dans Bronze ; `ignored_fields` liste les champs météo
non consommés suivis par le transformateur, pas un inventaire exhaustif du JSON.
Le contrôle vérifie le contrat existant ; les tests de valeurs des règles
restent nécessaires pour détecter une erreur métier partagée avec l'audit.

## Exécuter sur les données déployées

Après déploiement Terraform, ouvrir
`/Shared/weather-silver/silver_check` sur Databricks serverless et renseigner :

| Widget | Valeur |
| --- | --- |
| `bronze_root` | `abfss://bronze@<compte>.dfs.core.windows.net` |
| `silver_root` | `abfss://datalake@<compte>.dfs.core.windows.net/silver/weatherapi` |
| `collected_before` | Date exclusive avec fuseau, par exemple `2026-11-13T00:00:00Z` |
| `batch_size` | `100` par défaut |

Choisir une borne antérieure à la dernière fenêtre de publication achevée
(publication quotidienne à 02 h UTC). Une collecte déjà reçue mais encore
en attente dans la période échoue volontairement comme publication manquante ;
les collectes plus récentes que la borne sont exclues des deux côtés.
La borne utilise l'heure de l'identifiant canonique Bronze, même pour une
enveloppe rejetée dépourvue de timestamp fiable.

Le premier résultat résume le statut, les versions lues, le nombre d'archives
contrôlées et les anomalies. La table suivante détaille chaque archive, les
créneaux reçus, les exclusions, rejets, doublons et écarts de stockage.
`AssertionError` rend l'échec visible au niveau de la tâche. Une table Silver
absente ou illisible provoque une erreur technique, jamais un résultat vert.

Depuis un notebook Python :

```python
# Dans une cellule séparée : %run /Shared/weather-silver/silver_audit
report = audit(
    spark,
    bronze_root=bronze_root,
    silver_root=silver_root,
    collected_before=datetime(2026, 11, 13, tzinfo=timezone.utc),
)
assert report["ok"], report["errors"]
```

## Tests reproductibles

```sh
python -m pip install -r requirements-databricks.txt
python -m pytest databricks/tests -q -o testpaths=databricks/tests
```

`test_reconciliation.py` couvre les captures dérivées réelles, les exclusions
géographiques, conflits, doublons, métriques invalides, sections absentes,
échecs de collecte et enveloppes rejetées. Il injecte aussi des pertes,
altérations et diagnostics manquants, dont une perte compensée par un doublon.
Une transformation volontairement défectueuse prouve que le recomptage Bronze
détecte une heure ignorée par le parseur.

`test_reconciliation_delta.py` exécute l'audit sur les tables Delta réellement
écrites par le pipeline, vérifie les lots, la borne de collecte, les sources
orphelines, les archives non publiées et l'absence de commit pendant l'audit.
Le workflow existant `Test Silver notebooks` inclut ces tests et refuse que les
tests Delta soient ignorés faute de JARs Maven.

Ces tests locaux ne constituent pas une exécution sur l'historique ADLS de
production : celle-ci nécessite l'accès au workspace Databricks et au stockage.
