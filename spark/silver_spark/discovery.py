"""List canonical Bronze archives and keep those without a publication.

The publications table is the only progress record: no cursor, no checkpoint.
Listing selects only file paths, so Bronze contents are read for pending files only.
"""
from dataclasses import dataclass

from pyspark.sql import functions as F

from shared.config import BRONZE_PATH_PREFIX

from .engine import Source
from .schemas import PUBLICATIONS

CANONICAL = r"(weather/weatherapi/\d{4}/\d{2}/\d{2}/\d{2}/((\d{8}_\d{6}(_[0-9a-f]{32})?))_weather_data\.json)$"


@dataclass(frozen=True)
class Discovery:
    pending: list
    pending_total: int
    listed: int
    ignored: int

    def to_dict(self):
        return {"listed": self.listed, "ignored_non_canonical": self.ignored,
                "pending_total": self.pending_total, "selected": len(self.pending)}


def list_bronze(spark, bronze_root):
    files = (spark.read.format("binaryFile")
             .option("recursiveFileLookup", "true")
             .option("pathGlobFilter", "*_weather_data.json")
             .load(f"{bronze_root.rstrip('/')}/{BRONZE_PATH_PREFIX}")
             .select("path"))
    return (files
            .withColumn("source_blob", F.regexp_extract("path", CANONICAL, 1))
            .withColumn("ingestion_id", F.regexp_extract("path", CANONICAL, 2)))


def discover(spark, paths, bronze_root, *, limit, reconcile=False):
    """Pending canonical sources, oldest first, at most ``limit``.

    ``reconcile=True`` returns published ingestions too, so the engine re-reads
    them and fails visibly if a Bronze object changed under a published ID.
    """
    listing = list_bronze(spark, bronze_root)
    canonical = listing.where(F.col("source_blob") != "")
    if not reconcile:
        done = spark.read.format("delta").load(paths.table(PUBLICATIONS)).select("ingestion_id")
        canonical = canonical.join(done, "ingestion_id", "left_anti")
    counts = listing.agg(
        F.count(F.lit(1)).alias("listed"),
        F.sum(F.when(F.col("source_blob") == "", 1).otherwise(0)).alias("ignored"),
    ).collect()[0]
    pending_total = canonical.count()
    selected = canonical.orderBy("source_blob").limit(limit).select("path", "source_blob").collect()
    return Discovery([(r["path"], r["source_blob"]) for r in selected], pending_total,
                     int(counts["listed"] or 0), int(counts["ignored"] or 0))


def load_sources(spark, selected):
    """Read the exact bytes of the selected Bronze files (binaryFile keeps them intact)."""
    if not selected:
        return []
    relative = dict(selected)
    rows = spark.read.format("binaryFile").load(list(relative)).select("path", "content").collect()
    sources = [Source(relative[r["path"]], bytes(r["content"])) for r in rows]
    if len(sources) != len(relative):
        raise RuntimeError("Some selected Bronze files could not be read")
    return sources
