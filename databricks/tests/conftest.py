"""Load the Databricks notebooks as Python modules, exactly as Databricks runs them.

A notebook source file is plain Python split by ``# COMMAND ----------``. Cells
made only of ``# MAGIC`` lines are markdown, except ``%run ./other``, which runs
the other notebook in the same namespace, as Databricks does. Nothing is copied:
the tests exercise the notebook files that Terraform uploads.
"""
import json
from pathlib import Path
import sys
import types

import pytest

NOTEBOOKS = Path(__file__).resolve().parents[1] / "notebooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "weatherapi"
REAL_FIXTURES = ("bronze_success_20261017_derived_real.json", "bronze_success_20261018_derived_real.json")
CELL = "# COMMAND ----------"


def _execute(name, namespace, stack=()):
    path = NOTEBOOKS / f"{name}.py"
    if name in stack:
        raise RecursionError(f"%run cycle through {name}")
    source = path.read_text(encoding="utf-8")
    if not source.startswith("# Databricks notebook source\n"):
        raise ValueError(f"{path} is not a Databricks notebook source file")
    for index, cell in enumerate(source.split(CELL)):
        lines = [line for line in cell.strip("\n").splitlines() if line.strip()]
        if lines and lines[0] == "# Databricks notebook source":
            lines = lines[1:]
        if not lines:
            continue
        if all(line.startswith("# MAGIC") for line in lines):
            command = lines[0].removeprefix("# MAGIC").strip()
            if command.startswith("%run "):
                _execute(command.removeprefix("%run ").strip().removeprefix("./"), namespace, (*stack, name))
            continue
        exec(compile("\n".join(lines) + "\n", f"{path}#cell{index}", "exec"), namespace)


def load_notebook(name):
    module = types.ModuleType(name)
    module.__file__ = str(NOTEBOOKS / f"{name}.py")
    sys.modules[name] = module  # dataclasses resolve their module by name
    _execute(name, module.__dict__)
    return module


# Library notebooks are importable by tests: ``from silver_rules import transform_bronze``.
silver_rules = load_notebook("silver_rules")
silver_pipeline = load_notebook("silver_pipeline")


def _session(with_delta):
    from pyspark.sql import SparkSession
    builder = (SparkSession.builder.master("local[2]").appName("silver-notebook-tests")
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
    except Exception:  # Maven unreachable: Delta tests are skipped, never faked.
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
        result.append((silver_rules.bronze_blob_name(value["ingestion_id"], value["ingestion_timestamp"]), raw))
    return result
