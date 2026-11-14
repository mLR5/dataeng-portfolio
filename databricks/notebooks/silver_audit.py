# Databricks notebook source
# MAGIC %md
# MAGIC # Audit historique Bronze / Silver
# MAGIC Lecture seule, bornée par une date de collecte exclusive explicite.

# COMMAND ----------

# MAGIC %run ./silver_pipeline

# COMMAND ----------

# MAGIC %run ./silver_reconciliation

# COMMAND ----------

def audit(spark, *, bronze_root, silver_root, collected_before, batch_size=100):
    """Read every Bronze before a cutoff and reconcile frozen Silver versions.

    Run after publication, with a cutoff older than the daily publication window.
    No ensure_tables, MERGE, write, repair, provider call or automatic scheduling.
    Listing includes the whole history; only batches of Bronze bytes/weather rows
    reach the driver. Per-source reports contain counts and diagnostics only.
    """
    if not isinstance(collected_before, datetime) or collected_before.tzinfo is None:
        raise ValueError("collected_before must be an explicit timezone-aware cutoff")
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    cutoff_id = collected_before.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    paths = SilverPaths(silver_root)
    # Publication is frozen first: later table snapshots include everything it
    # promises, even if an insert-only writer appends a newer ingestion meanwhile.
    versions, frames = {}, {}
    for name in (PUBLICATIONS, OBSERVATION_TABLE, FORECAST_TABLE, QUARANTINE):
        versions[name] = _version(spark, paths.table(name))
        frames[name] = (spark.read.format("delta").option("versionAsOf", versions[name])
                        .load(paths.table(name)))
        if [(f.name, f.dataType) for f in frames[name].schema] != [
                (f.name, f.dataType) for f in SPARK_SCHEMAS[name]]:
            raise ValueError(f"Stored schema differs from contract: {name}")

    listed = list_bronze(spark, bronze_root).collect()
    selected = [r for r in listed if r["ingestion_id"] and r["ingestion_id"][:15] < cutoff_id]
    counts = Counter(r["ingestion_id"] for r in selected)
    errors = []
    noncanonical = sum(not r["ingestion_id"] for r in listed)
    if noncanonical:
        errors.append("bronze: noncanonical_archive_paths")
    if any(n > 1 for n in counts.values()):
        errors.append("bronze: duplicate_ingestion_identity")
    if not selected:
        errors.append("bronze: no_sources_in_scope")

    ids = sorted(counts)
    inventory = spark.createDataFrame([(i,) for i in ids], "ingestion_id string")
    orphans = {}
    for name, frame in frames.items():
        # Use the ID (also valid for rejected envelopes without collected_at_utc),
        # not publication time, to put both sides in the same collection scope.
        in_scope = frame.where(F.substring("ingestion_id", 1, 15) < F.lit(cutoff_id))
        missing_bronze = (in_scope.select("ingestion_id").distinct()
                          .join(inventory, "ingestion_id", "left_anti"))
        orphans[name] = missing_bronze.count()
        if orphans[name]:
            errors.append(f"{name}: ingestion_without_bronze")
        if frame.where(F.col("ingestion_id").isNull()).limit(1).count():
            errors.append(f"{name}: null_ingestion_identity")

    reports = []
    selected.sort(key=lambda r: r["ingestion_id"])
    for start in range(0, len(selected), batch_size):
        batch = selected[start:start + batch_size]
        sources = load_sources(spark, [(r["path"], r["source_blob"]) for r in batch])
        batch_ids = [s.ingestion_id for s in sources]
        actual = {}
        for name, frame in frames.items():
            grouped = defaultdict(list)
            for row in frame.where(F.col("ingestion_id").isin(batch_ids)).collect():
                grouped[row["ingestion_id"]].append(row.asDict())
            actual[name] = grouped
        for source in sources:
            ingestion_id = source.ingestion_id
            report = reconcile_source(source.raw, source.source_blob, tables={
                name: pa.Table.from_pylist(actual[name][ingestion_id], schema=schema)
                for name, schema in TABLE_SCHEMAS.items()
            }, publications=actual[PUBLICATIONS][ingestion_id],
                quarantine=actual[QUARANTINE][ingestion_id])
            for publication in actual[PUBLICATIONS][ingestion_id]:
                for table, field in ((OBSERVATION_TABLE, "observation_version"),
                                     (FORECAST_TABLE, "forecast_version")):
                    if not 0 <= publication[field] <= versions[table]:
                        report["errors"].append(f"publications: invalid_reference:{field}")
                        report["ok"] = False
            reports.append(report)
    failed = sum(not r["ok"] for r in reports)
    return {
        "ok": not errors and not failed, "collected_before": collected_before.isoformat(),
        "silver_versions": versions, "checked_sources": len(reports),
        "failed_sources": failed, "noncanonical_archives": noncanonical,
        "orphan_ingestions": orphans, "errors": errors,
        "statuses": dict(Counter(r["status"] for r in reports)), "sources": reports,
    }
