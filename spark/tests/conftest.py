import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "functions", ROOT / "spark"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shared.claim_check import bronze_blob_name  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "weatherapi"
REAL_FIXTURES = ("bronze_success_20261017_derived_real.json", "bronze_success_20261018_derived_real.json")


def _session(with_delta):
    from pyspark.sql import SparkSession
    builder = (SparkSession.builder.master("local[2]").appName("silver-spark-tests")
               .config("spark.sql.session.timeZone", "UTC")
               .config("spark.ui.enabled", "false")
               .config("spark.sql.shuffle.partitions", "2"))
    if with_delta:
        from delta import configure_spark_with_delta_pip
        builder = configure_spark_with_delta_pip(
            builder.config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
                   .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog"))
    return builder.getOrCreate()


@pytest.fixture(scope="session")
def spark_session():
    """Delta-enabled when the Delta jars can be resolved (CI), plain Spark otherwise."""
    try:
        session = _session(True)
        session.sql("SELECT 1").collect()
        session.delta_enabled = True
    except Exception:  # Maven unreachable: Delta tests are skipped, not faked.
        session = _session(False)
        session.delta_enabled = False
    yield session
    session.stop()


@pytest.fixture
def spark(spark_session):
    return spark_session


@pytest.fixture
def delta_spark(spark_session):
    if not spark_session.delta_enabled:
        pytest.skip("Delta Lake jars unavailable in this environment")
    return spark_session


def write_bronze(root, raw, source_blob):
    target = Path(root) / source_blob
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target


def real_sources():
    """(source_blob, raw bytes) of the two Bronze fixtures derived from real captures."""
    result = []
    for name in REAL_FIXTURES:
        raw = (FIXTURES / name).read_bytes()
        value = json.loads(raw)
        result.append((bronze_blob_name(value["ingestion_id"], value["ingestion_timestamp"]), raw))
    return result
