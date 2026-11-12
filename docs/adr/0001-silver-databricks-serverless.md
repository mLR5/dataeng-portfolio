# ADR 0001 — Exécuter Silver sur Databricks serverless plutôt que dans la Function App

- Statut : accepté, mise en œuvre phases 1 à 3
- Date : 2026-11-11
- Remplace : le runtime Silver S4 de la branche `feat/silver-runtime-s4` (non fusionné)

## Contexte

Bronze tourne en production dans la Function App Flex Consumption. Silver existe
sur `main` sous forme de bibliothèque Python validée (S1 contrats, S2
transformation pure, S3 écriture Delta avec delta-rs, acceptée sur ADLS). Le
runtime planifié S4 (timer 02 h UTC, file de continuations, checkpoint, sonde de
santé) est resté sur une branche : les tables Silver définitives n'ont jamais été
initialisées. Aucune donnée de production n'est donc à migrer.

Les branches `audit/databricks-serverless-readiness`,
`claude/gallant-brahmagupta-p2hg55` et `probe/databricks-serverless` ont montré
qu'un workspace `computeMode: Serverless` se crée en francecentral, que le
principal de service CI y devient administrateur et qu'un metastore Unity Catalog
est déjà assigné.

## Décision

1. **Ne pas fusionner S4.** La branche reste une référence et un repli.
2. **Silver devient un job Databricks serverless quotidien**, Bronze reste dans la
   Function App.
3. **Le transformateur S2 est réutilisé tel quel** (wheel), sans réécriture en
   Spark SQL : les 75 cas S2 restent la référence des règles métier.
4. **La table Delta `publications` est l'unique registre d'avancement.** Chaque
   exécution liste Bronze, retire les ingestions déjà publiées et traite le reste.
   Il n'y a ni Auto Loader, ni checkpoint de streaming, ni lease.
5. **Une seule exécution à la fois** (`max_concurrent_runs = 1`) et l'isolation
   optimiste de Delta remplacent le lease Blob coopératif de S3.
6. **Les garanties S3 sont conservées** : MERGE insert-only sur les mêmes clés,
   refus d'un snapshot existant différent, publication écrite après les deux
   tables, filtre Gold par publications complètes.

## Pourquoi pas Auto Loader

Le plan initial prévoyait Auto Loader avec `foreachBatch`. Sur serverless, seul
`Trigger.AvailableNow` est accepté et le `foreachBatch` Python s'exécute via Spark
Connect avec des limites signalées. Surtout, un checkpoint de streaming serait un
second état d'avancement à garder cohérent avec les publications. Au volume
actuel (24 fichiers par jour), lister Bronze et faire une anti-jointure sur les
publications est simple, sans état caché et se répare seul après une panne.

## Conséquences

- Supprimé par rapport à S4 : file `silver-runtime`, coordinateur, curseurs,
  budget par invocation, lease, sonde de santé Function App.
- Ajouté : workspace serverless, Access Connector, objets Unity Catalog, bundle
  Databricks, un job à deux tâches (traitement puis contrôle de fraîcheur).
- Le coût n'est pas un argument : chaque exécution est facturée en DBU, alors que
  la Function App coûte presque rien à ce volume. Le coût réel se lit dans
  `system.billing.usage` dès le premier job ; s'il dépasse le budget du
  portfolio, S4 redevient l'option.
- Les tables produites par Spark doivent rester lisibles par delta-rs 1.2.1
  (fonctionnalités Delta fixées explicitement, test en CI).

## Validation restante (phases 4 et 5)

Exécution sur tout l'historique Bronze vers une racine isolée, comparaison ligne
à ligne avec `scripts/silver_backfill.py`, rejeu sans nouveau commit, puis 7 jours
de fonctionnement planifié avant d'initialiser la racine définitive.
