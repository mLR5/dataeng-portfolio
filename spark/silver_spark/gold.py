"""Gold read barrier on the Spark publications table (same rule as S3).

Freeze the complete publications, read each Silver table at the maximum
published version and filter on these ingestion IDs. Insert-only tables mean
the maximum version contains every earlier publication. Never glob Parquet.
"""
from pyspark.sql import functions as F

from shared.silver_checks import PublicationConflict
from shared.silver_contract import FORECAST_TABLE, OBSERVATION_TABLE, SCHEMA_VERSION, TRANSFORM_VERSION

from .schemas import PUBLICATIONS


def published_read_plan(spark, paths, *, collected_before=None):
    publications = spark.read.format("delta").load(paths.table(PUBLICATIONS)).where("complete")
    if collected_before is not None:
        publications = publications.where(F.col("collected_at_utc") < F.lit(collected_before))
    rows = publications.select(
        "ingestion_id", "schema_version", "transform_version",
        "observation_version", "forecast_version").collect()
    ids = set()
    for row in rows:
        if row["schema_version"] != SCHEMA_VERSION or row["transform_version"] != TRANSFORM_VERSION:
            raise PublicationConflict("Gold cannot mix transformation versions")
        if row["ingestion_id"] in ids:
            raise PublicationConflict("Duplicate publication identity")
        ids.add(row["ingestion_id"])
    versions = {
        OBSERVATION_TABLE: max((r["observation_version"] for r in rows), default=None),
        FORECAST_TABLE: max((r["forecast_version"] for r in rows), default=None),
    }
    return {"ingestion_ids": sorted(ids), "tables": {
        name: {"path": paths.table(name), "version": version}
        for name, version in versions.items() if version is not None
    }}


def read_published(spark, plan, table_name):
    spec = plan["tables"][table_name]
    return (spark.read.format("delta").option("versionAsOf", spec["version"]).load(spec["path"])
            .where(F.col("ingestion_id").isin(plan["ingestion_ids"])))
