# Databricks notebook source
# MAGIC %md
# MAGIC # Contrôle de non-perte Bronze → Silver
# MAGIC
# MAGIC Lancer après publication. La borne exclusive doit précéder la dernière
# MAGIC fenêtre de publication achevée (le job quotidien est à 02 h UTC).
# MAGIC Un résultat vert signifie « conforme au contrat », y compris les rejets
# MAGIC explicités ; il ne certifie pas que toutes les collectes sont exploitables.

# COMMAND ----------

# MAGIC %run ./silver_audit

# COMMAND ----------

dbutils.widgets.text("bronze_root", "", "Racine Bronze")
dbutils.widgets.text("silver_root", "", "Racine Silver")
dbutils.widgets.text("collected_before", "", "Collectes avant (exclusif, ISO UTC)")
dbutils.widgets.text("batch_size", "100", "Archives par lot")
spark.conf.set("spark.sql.session.timeZone", "UTC")
cutoff = dbutils.widgets.get("collected_before")
if not cutoff or not re.search(r"(Z|[+-]\d{2}:\d{2})$", cutoff):
    raise ValueError("Renseigner collected_before avec un fuseau explicite, ex. 2026-11-13T00:00:00Z")
report = audit(spark, bronze_root=dbutils.widgets.get("bronze_root"),
               silver_root=dbutils.widgets.get("silver_root"),
               collected_before=parse_timestamp(cutoff),
               batch_size=int(dbutils.widgets.get("batch_size")))
print(json.dumps({k: v for k, v in report.items() if k != "sources"}, sort_keys=True))
details = [{
    "source_blob": source["source_blob"], "status": source["status"], "ok": source["ok"],
    "details_json": json.dumps(source, sort_keys=True),
} for source in report["sources"]]
display(spark.createDataFrame(details, "source_blob string, status string, ok boolean, details_json string"))
if not report["ok"]:
    raise AssertionError("Écarts Bronze/Silver inexpliqués : consulter le rapport de réconciliation")
