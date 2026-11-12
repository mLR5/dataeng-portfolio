"""Entry points of the Databricks job: ``silver-run`` then ``silver-freshness``.

Only fixed counters and status names are printed: no Bronze contents, no secrets.
A non-zero exit fails the task, so the job retries and notifies.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from .discovery import discover, load_sources
from .engine import ensure_tables, process_sources
from .schemas import PUBLICATIONS, SilverPaths


def _spark():
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    return spark


def _log(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True, default=str), flush=True)


def run(spark, *, bronze_root, silver_root, batch_size=200, max_batches=50, reconcile=False):
    paths = SilverPaths(silver_root)
    ensure_tables(spark, paths)
    found = discover(spark, paths, bronze_root, limit=batch_size * max_batches, reconcile=reconcile)
    _log("SILVER_DISCOVERY", **found.to_dict())
    totals = {"published": 0, "already_processed": 0, "batches": 0, "statuses": {}}
    for start in range(0, len(found.pending), batch_size):
        batch = process_sources(spark, paths, load_sources(spark, found.pending[start:start + batch_size]))
        totals["batches"] += 1
        totals["published"] += len(batch.published)
        totals["already_processed"] += len(batch.already_processed)
        for status, count in batch.statuses.items():
            totals["statuses"][status] = totals["statuses"].get(status, 0) + count
        _log("SILVER_BATCH", **batch.to_dict())
    # Not an error: the next run continues from the publications table.
    totals["remaining"] = found.pending_total - len(found.pending)
    _log("SILVER_RUN", **totals)
    return totals


def freshness(spark, *, silver_root, max_age_hours=26, min_daily=20, now=None):
    """Fail when the newest publication is too old or the last 24 h are too thin."""
    now = now or datetime.now(timezone.utc)
    publications = spark.read.format("delta").load(SilverPaths(silver_root).table(PUBLICATIONS))
    row = publications.agg(
        F.max("collected_at_utc").alias("latest"),
        F.sum(F.when(F.col("collected_at_utc") >= F.lit(now - timedelta(hours=24)), 1).otherwise(0)).alias("last_24h"),
        F.sum(F.when((F.col("collected_at_utc") >= F.lit(now - timedelta(hours=24)))
                     & F.col("status").isin("rejected", "empty"), 1).otherwise(0)).alias("unusable_24h"),
    ).collect()[0]
    latest = row["latest"]
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    report = {
        "latest_collected_at_utc": latest.isoformat() if latest else None,
        "publications_last_24h": int(row["last_24h"] or 0),
        "unusable_last_24h": int(row["unusable_24h"] or 0),
        "max_age_hours": max_age_hours, "min_daily": min_daily,
    }
    report["fresh"] = bool(latest and now - latest <= timedelta(hours=max_age_hours)
                           and report["publications_last_24h"] >= min_daily)
    _log("SILVER_FRESHNESS", **report)
    return report


def _parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--silver-root", required=True)
    return parser


def main_run(argv=None):
    parser = _parser("Publish pending Bronze ingestions into Silver Delta tables.")
    parser.add_argument("--bronze-root", required=True)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-batches", type=int, default=50)
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not 1 <= args.batch_size <= 1000 or not 1 <= args.max_batches <= 500:
        parser.error("batch size 1..1000, max batches 1..500")
    run(_spark(), bronze_root=args.bronze_root, silver_root=args.silver_root,
        batch_size=args.batch_size, max_batches=args.max_batches, reconcile=args.reconcile)


def main_freshness(argv=None):
    parser = _parser("Fail when Silver publications are stale.")
    parser.add_argument("--max-age-hours", type=float, default=26)
    parser.add_argument("--min-daily", type=int, default=20)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    report = freshness(_spark(), silver_root=args.silver_root,
                       max_age_hours=args.max_age_hours, min_daily=args.min_daily)
    if not report["fresh"]:
        raise SystemExit("Silver publications are stale")
