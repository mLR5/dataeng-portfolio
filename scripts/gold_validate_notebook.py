# Databricks notebook source
# MAGIC %md
# MAGIC # Validation isolée Gold : runtime, historique complet et incrémental
# MAGIC
# MAGIC Notebook temporaire importé avec les composants Gold dans un dossier de
# MAGIC validation. Toutes les écritures restent sous `gold/_validation/`.
# MAGIC Les cinq tables Silver sont figées une fois ; la production reste en lecture.

# COMMAND ----------
# MAGIC %run ./gold_pipeline
# COMMAND ----------

for name in ("silver_root", "validation_root", "reference_root", "incremental_root", "cut_date",
             "production_root", "gold_root", "uc_schema"):
    dbutils.widgets.text(name, "")
dbutils.widgets.dropdown("operation", "validation", ["validation", "production_verify"])

# COMMAND ----------

def _gv_roots(silver_root, validation_root, reference_root, incremental_root):
    source = SilverPaths(silver_root).root
    validation = SilverPaths(validation_root).root
    reference = SilverPaths(reference_root).root
    incremental = SilverPaths(incremental_root).root
    if "/gold/_validation/" not in validation:
        raise ValueError("Validation writes require an isolated gold/_validation/ root")
    if not all(root.startswith(validation + "/") for root in (reference, incremental)):
        raise ValueError("Reference and incremental roots must belong to validation_root")
    children = (reference, incremental, validation + "/smoke", validation + "/silver_snapshot")
    for index, root in enumerate(children):
        if root == source or root.startswith(source + "/") or source.startswith(root + "/"):
            raise ValueError("Validation outputs must be separate from production Silver")
        for other in children[index + 1:]:
            if root == other or root.startswith(other + "/") or other.startswith(root + "/"):
                raise ValueError("Validation child roots must be distinct and non-overlapping")
    return source, validation, reference, incremental


def _gv_physical_versions(spark, root):
    paths = GoldPaths(root)
    return {name: _version(spark, paths.table(name)) for name in (*GOLD_SCHEMAS, "builds")}


def validate_gold_history(spark, *, silver_root, validation_root, reference_root,
                          incremental_root, cut_date=""):
    source, validation, reference, incremental = _gv_roots(
        silver_root, validation_root, reference_root, incremental_root)
    smoke = validation + "/smoke"
    snapshot = validation + "/silver_snapshot"
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    # A unique run root prevents a prior source ledger from turning a requested
    # full-vs-incremental proof into an accidental replay of unrelated data.
    for root in (reference, incremental, smoke):
        if latest_gold_build(spark, GoldPaths(root)) is not None:
            raise ValueError("Validation requires fresh output roots")
    snapshot_paths = SilverPaths(snapshot)
    for name in SPARK_SCHEMAS:
        if DeltaTable.isDeltaTable(spark, snapshot_paths.table(name)):
            raise ValueError("Validation requires a fresh frozen Silver root")

    frozen, source_ids, fingerprints, source_versions, _ = _gold_inputs(
        spark, source, None, None, None)
    if not source_ids:
        raise ValueError("No complete Silver sources available for historical validation")
    span = frozen[PUBLICATIONS].agg(
        F.min("collected_at_utc").alias("first"),
        F.max("collected_at_utc").alias("last")).first()
    if span["first"] is None or span["last"] is None:
        raise ValueError("Historical validation requires dated source publications")
    first_day, last_day = span["first"].date(), span["last"].date()
    if first_day >= last_day:
        raise ValueError("Historical two-period proof requires sources on multiple UTC dates")
    if cut_date:
        cut, _ = _gold_dates(cut_date, "")
        cut_day = cut.date()
    else:
        # Midpoint of the frozen history; this is a source-collection boundary.
        cut_day = first_day + timedelta(days=max(1, (last_day - first_day).days // 2))
        cut_date = cut_day.isoformat()
    if not first_day < cut_day <= last_day:
        raise ValueError("cut_date must divide the frozen source history into two nonempty periods")

    _log("GOLD_VALIDATION_FREEZE", source_count=len(source_ids), versions=source_versions,
         first_collection_date=first_day, last_collection_date=last_day, cut_date=cut_date)
    # Copies preserve row contents, source fingerprints and summaries. Only the
    # publication's storage-version pointers change to target the immutable copy.
    copied_versions = {}
    for name in SILVER_DATA_TABLES:
        (frozen[name].write.format("delta").mode("error")
         .option("delta.enableDeletionVectors", "false").save(snapshot_paths.table(name)))
        copied_versions[name] = _version(spark, snapshot_paths.table(name))
    (frozen[QUARANTINE].write.format("delta").mode("error")
     .option("delta.enableDeletionVectors", "false").save(snapshot_paths.table(QUARANTINE)))
    publications = frozen[PUBLICATIONS]
    for name, version_column in PUBLICATION_VERSION_COLUMNS.items():
        publications = publications.withColumn(version_column, F.lit(copied_versions[name]).cast("long"))
    (publications.write.format("delta").mode("error")
     .option("delta.enableDeletionVectors", "false").save(snapshot_paths.table(PUBLICATIONS)))
    copied_ids = {row[0] for row in spark.read.format("delta").load(
        snapshot_paths.table(PUBLICATIONS)).select("ingestion_id").collect()}
    if copied_ids != set(source_ids):
        raise AssertionError("Frozen Silver copy changed the source identities")

    # A cheap first-day build catches runtime/Connect/Delta incompatibilities
    # before spending compute on three materialisations of the full-history proof.
    smoke_report = run_gold(spark, silver_root=snapshot, gold_root=smoke,
        start_date=first_day.isoformat(), end_date=(first_day + timedelta(days=1)).isoformat(),
        minimum_coverage_pct=0)
    _log("GOLD_VALIDATION_SMOKE", **smoke_report)
    full_report = run_gold(spark, silver_root=snapshot, gold_root=reference,
                           mode="full", minimum_coverage_pct=98)
    _log("GOLD_VALIDATION_FULL", **full_report)
    prefix_report = run_gold(spark, silver_root=snapshot, gold_root=incremental,
                             end_date=cut_date, minimum_coverage_pct=0)
    if not 0 < prefix_report["source_count"] < len(source_ids):
        raise AssertionError("First incremental period did not select a proper nonempty subset")
    # Unbounded admission adds every remaining frozen ID, including a possible
    # undated rejected envelope. It retains the prefix ledger unchanged.
    cumulative_report = run_gold(spark, silver_root=snapshot, gold_root=incremental,
                                 minimum_coverage_pct=98)
    if full_report["source_count"] != len(source_ids) or cumulative_report["source_count"] != len(source_ids):
        raise AssertionError("Full and cumulative builds must cover every frozen Silver source")
    if cumulative_report["new_sources"] != len(source_ids) - prefix_report["source_count"]:
        raise AssertionError("Incremental source ledger counted an unexpected set of additions")

    full_plan = gold_read_plan(spark, reference)
    cumulative_plan = gold_read_plan(spark, incremental)
    compared = {}
    for name in GOLD_SCHEMAS:
        expected = read_gold(spark, full_plan, name)
        actual = read_gold(spark, cumulative_plan, name)
        if expected.exceptAll(actual).limit(1).count() or actual.exceptAll(expected).limit(1).count():
            raise AssertionError(f"Full and cumulative Gold outputs differ: {name}")
        compared[name] = full_report["row_counts"][name]
    if full_report["row_counts"] != cumulative_report["row_counts"]:
        raise AssertionError("Full and cumulative row counts differ")
    if full_report["quality"] != cumulative_report["quality"]:
        raise AssertionError("Full and cumulative coverage/quality results differ")
    for root in (reference, incremental):
        build = latest_gold_build(spark, GoldPaths(root))
        if set(json.loads(build["source_ids_json"])) != set(source_ids):
            raise AssertionError("Completed Gold manifest omits a frozen Silver source")
        if json.loads(build["source_fingerprints_json"]) != fingerprints:
            raise AssertionError("Completed Gold manifest changed source fingerprints")

    before = _gv_physical_versions(spark, incremental)
    replay_report = run_gold(spark, silver_root=snapshot, gold_root=incremental,
                            minimum_coverage_pct=98)
    if replay_report["changed"] or replay_report["build_id"] != cumulative_report["build_id"]:
        raise AssertionError("Replay unexpectedly published another Gold build")
    if _gv_physical_versions(spark, incremental) != before:
        raise AssertionError("Replay unexpectedly committed a Gold table or control version")
    report = {
        "status": "passed", "silver_root": source, "frozen_silver_root": snapshot,
        "source_count": len(source_ids), "source_versions": source_versions,
        "cut_date": cut_date, "smoke": smoke_report,
        "full": full_report, "incremental_prefix": prefix_report,
        "incremental_cumulative": cumulative_report, "replay": replay_report,
        "compared_tables": compared, "reference_root": reference,
        "incremental_root": incremental,
    }
    _log("GOLD_VALIDATION_COMPLETE", **report)
    return report


def verify_gold_production(spark, *, silver_root, gold_root, uc_schema=""):
    """Read-only verification of the production job's completed Gold output."""
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    silver_paths, gold_paths = SilverPaths(silver_root), GoldPaths(gold_root)
    build = latest_gold_build(spark, gold_paths)
    if build is None:
        raise AssertionError("Production job did not publish a completed Gold build")
    if build["gold_version"] != GOLD_VERSION or build["silver_root"] != silver_paths.root:
        raise AssertionError("Production manifest points to an unexpected source or Gold version")
    plan = gold_read_plan(spark, gold_paths.root)
    tables = {name: read_gold(spark, plan, name) for name in GOLD_SCHEMAS}
    counts = validate_gold(spark, tables)
    quality = gold_quality(tables)
    _enforce_gold_coverage(quality, 98)
    if counts != json.loads(build["row_counts_json"]) or quality != json.loads(build["quality_json"]):
        raise AssertionError("Production contents differ from their completed manifest")
    ids = set(json.loads(build["source_ids_json"]))
    expected_fingerprints = json.loads(build["source_fingerprints_json"])
    complete = spark.read.format("delta").load(silver_paths.table(PUBLICATIONS)).where("complete")
    source_rows = complete.select("ingestion_id", "source_blob", "source_sha256").collect()
    if len({row["ingestion_id"] for row in source_rows}) != len(source_rows):
        raise AssertionError("Production Silver contains duplicate publication identities")
    actual_fingerprints = {row["ingestion_id"]: [row["source_blob"], row["source_sha256"]]
                           for row in source_rows if row["ingestion_id"] in ids}
    if set(actual_fingerprints) != ids or actual_fingerprints != expected_fingerprints:
        raise AssertionError("Production Gold ledger differs from consumed Silver publications")
    metadata = {}
    for name, columns in GOLD_CLUSTER_COLUMNS.items():
        path = gold_paths.table(name)
        detail = spark.sql(f"DESCRIBE DETAIL {_delta(path)}").first().asDict()
        if _version(spark, path) != plan["tables"][name]["version"]:
            raise AssertionError(f"Production physical table differs from completed build: {name}")
        if str(detail["properties"].get("delta.enableDeletionVectors", "")).lower() != "false":
            raise AssertionError(f"Deletion vectors must remain disabled: {name}")
        if tuple(detail.get("clusteringColumns", ())) != tuple(columns):
            raise AssertionError(f"Production clustering differs from contract: {name}")
        if name == "fact_forecast_daily":
            constraint = detail["properties"].get("delta.constraints.lead_days_range", "")
            if re.sub(r"\s+", "", constraint).lower() != "lead_daysbetween-1and2":
                raise AssertionError("Production daily lead-days constraint is absent or unexpected")
        metadata[name] = {"version": plan["tables"][name]["version"],
                          "clustering_columns": list(columns)}
    registered = {}
    if uc_schema:
        if not _UC_SCHEMA.fullmatch(uc_schema):
            raise ValueError("uc_schema must be catalogue.schema")
        catalog, schema = uc_schema.split(".")
        for name in GOLD_SCHEMAS:
            qualified = f"`{catalog}`.`{schema}`.`{name}`"
            if not spark.catalog.tableExists(qualified):
                raise AssertionError(f"Production Unity Catalog table missing: {name}")
            if spark.catalog.getTable(qualified).tableType.upper() != "EXTERNAL":
                raise AssertionError(f"Production Unity Catalog table must be external: {name}")
            location = spark.sql(f"DESCRIBE DETAIL {qualified}").first()["location"]
            if not _same_location(location, gold_paths.table(name)):
                raise AssertionError(f"Production Unity Catalog location differs: {name}")
            registered[name] = f"{uc_schema}.{name}"
    report = {"status": "passed", "operation": "production_verify", "build_id": build["build_id"],
              "build_sequence": build["build_sequence"], "source_count": len(ids),
              "pending_complete_sources": len(source_rows) - len(ids), "row_counts": counts,
              "quality": quality, "metadata": metadata, "registered_tables": registered}
    _log("GOLD_PRODUCTION_VERIFIED", **report)
    return report

# COMMAND ----------

if dbutils.widgets.get("operation") == "production_verify":
    validation_report = verify_gold_production(spark,
        silver_root=dbutils.widgets.get("silver_root"),
        gold_root=dbutils.widgets.get("production_root") or dbutils.widgets.get("gold_root"),
        uc_schema=dbutils.widgets.get("uc_schema"))
else:
    validation_report = validate_gold_history(spark,
        silver_root=dbutils.widgets.get("silver_root"),
        validation_root=dbutils.widgets.get("validation_root"),
        reference_root=dbutils.widgets.get("reference_root"),
        incremental_root=dbutils.widgets.get("incremental_root"),
        cut_date=dbutils.widgets.get("cut_date"))
dbutils.notebook.exit(json.dumps(validation_report, sort_keys=True, default=str))
