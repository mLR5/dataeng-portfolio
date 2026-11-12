# ADR 0001 — Silver entièrement sur Databricks serverless, en notebooks

- Statut : accepté
- Date : 2026-11-12
- Remplace : le runtime Silver S4 (`feat/silver-runtime-s4`, non fusionné) et
  l'implémentation delta-rs S1 à S3 dans la Function App

## Contexte

Bronze tourne en production dans la Function App Flex Consumption. Silver existait
sur `main` sous forme de bibliothèque Python dans `functions/shared` (contrats S1,
transformation S2, écriture delta-rs S3), sans runtime planifié déployé : le
runtime S4 est resté sur une branche et les tables Silver définitives n'ont jamais
été initialisées. Aucune donnée de production n'est donc à migrer.

Les branches `audit/databricks-serverless-readiness`,
`claude/gallant-brahmagupta-p2hg55` et `probe/databricks-serverless` ont montré
qu'un workspace `computeMode: Serverless` se crée en francecentral, que le
principal de service CI y devient administrateur et qu'un metastore Unity Catalog
est déjà assigné.

## Décision

1. **La Function App ne sert qu'à ingérer Bronze.** Tout le code Silver en sort.
2. **Silver vit entièrement dans des notebooks Databricks** (`databricks/notebooks`) :
   règles S2 et contrat (`silver_rules`), pipeline Spark (`silver_pipeline`),
   tâches du job (`silver_publish`, `silver_freshness`) et exploration en lecture
   seule (`silver_explore`). Les bibliothèques sont partagées par `%run`.
3. **Databricks est une brique de l'infrastructure existante** : workspace, Unity
   Catalog, notebooks et job sont dans `infra/` et appliqués par le workflow
   *Deploy Infrastructure* habituel. Pas de workflow ni de bundle à part.
4. **La table Delta `publications` est l'unique registre d'avancement.** Chaque
   exécution liste Bronze, retire les ingestions déjà publiées et traite le reste :
   ni Auto Loader, ni checkpoint, ni lease.
5. **Une seule exécution à la fois** (`max_concurrent_runs = 1`) et l'isolation
   optimiste de Delta remplacent le lease Blob coopératif de S3.
6. **Les garanties S3 sont conservées** : MERGE insert-only sur les mêmes clés,
   refus d'un snapshot existant différent, publication écrite après les deux
   tables, filtre Gold par publications complètes.

## Pourquoi pas Auto Loader

Sur serverless, seul `Trigger.AvailableNow` est accepté et le `foreachBatch` Python
s'exécute via Spark Connect avec des limites signalées. Un checkpoint de streaming
serait surtout un second état d'avancement à garder cohérent avec les
publications. À 24 fichiers par jour, lister Bronze et faire une anti-jointure sur
les publications est simple, sans état caché et se répare seul.

## Conséquences

- Le contrat de chemin Bronze (`weather/weatherapi/AAAA/MM/JJ/HH/<id>_weather_data.json`,
  format des identifiants) appartient à la Function App ; le notebook
  `silver_rules` en garde une copie commentée qui doit suivre toute évolution.
- Les tests chargent les notebooks tels que Databricks les exécute (`%run` compris) :
  les 75 cas des règles S2 et les tests Delta du pipeline tournent en CI.
- Avant le retrait de delta-rs, la CI de la PR #10 a prouvé que le pipeline Spark
  produit exactement les mêmes lignes que le runner S3 sur les Bronze dérivés de
  captures réelles.
- Le coût n'est pas un argument : chaque exécution est facturée en DBU, alors que
  la Function App coûte presque rien à ce volume. Le coût réel se lit dans
  `system.billing.usage` dès le premier run de validation.

## Validation restante (phases 4 et 5)

Phase 4 : `DATABRICKS_ENABLED=true`, job sur une racine isolée, planning en pause,
exécution manuelle sur tout l'historique Bronze, relance sans nouveau commit,
lecture du coût. Phase 5 : racine définitive, planning actif à 02 h UTC, 7 jours
verts.
