"""Insert-only snapshots in two Delta tables, with explicit conflict checks."""
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc
from deltalake import CommitProperties, DeltaTable
from deltalake.exceptions import TableNotFoundError

from .silver_contract import TABLE_KEYS, TABLE_SCHEMAS


class SnapshotConflict(ValueError):
    """A key is duplicated or already has different immutable contents."""


@dataclass(frozen=True)
class TableCommit:
    path: str
    version: int
    row_count: int
    inserted: int

    def to_dict(self):
        return {'path': self.path, 'version': self.version,
                'row_count': self.row_count, 'inserted': self.inserted}


def _key(row, keys):
    value = tuple(row[k] for k in keys)
    if any(v is None for v in value):
        raise SnapshotConflict('Null snapshot key')
    return value


def _indexed(rows, keys):
    indexed = {}
    for row in rows:
        key = _key(row, keys)
        if key in indexed:
            raise SnapshotConflict('Duplicate snapshot key')
        indexed[key] = row
    return indexed


class DeltaSnapshotWriter:
    """One root per table, never per ingestion. No automatic schema evolution.

    Commit retries are disabled: callers restart from Bronze after a conflict,
    obtaining a new lease and reading a fresh Delta snapshot. A MERGE uses the
    exact target snapshot that passed immutable-content checks. Delta's optimistic
    conflict checking complements the cooperative lease, rather than relying on
    an unenforced primary-key constraint.
    """
    def __init__(self, root, *, storage_options=None):
        self.root = str(root).rstrip('/')
        self.storage_options = dict(storage_options or {})

    def table_path(self, table_name):
        if table_name not in TABLE_SCHEMAS:
            raise ValueError('Unknown Silver table')
        return f'{self.root}/{table_name}'

    def _open(self, table_name, lease):
        path = self.table_path(table_name)
        try:
            return DeltaTable(path, storage_options=self.storage_options)
        except TableNotFoundError:
            lease.assert_held()
            # Concurrent creation is a visible technical conflict, never an
            # overwrite. On retry the existing schema is validated below.
            return DeltaTable.create(
                path, TABLE_SCHEMAS[table_name], mode='error',
                storage_options=self.storage_options,
                configuration={'delta.isolationLevel': 'Serializable'},
            )

    def merge(self, table_name, rows, lease, *, ingestion_id):
        schema = TABLE_SCHEMAS[table_name]
        if not rows.schema.equals(schema, check_metadata=False):
            raise SnapshotConflict('Unexpected incoming Arrow schema')
        for field in schema:
            column = rows.column(field.name)
            if not field.nullable and column.null_count:
                raise SnapshotConflict('Null required snapshot field')
            if pa.types.is_floating(field.type) and not pc.all(pc.is_finite(column)).as_py() and column.null_count != rows.num_rows:
                raise SnapshotConflict('Non-finite snapshot value')
        expected = _indexed(rows.to_pylist(), TABLE_KEYS[table_name])
        if any(r['ingestion_id'] != ingestion_id for r in expected.values()):
            raise SnapshotConflict('Input contains another ingestion')
        lease.assert_held()
        target = self._open(table_name, lease)
        actual_schema = pa.schema(target.schema().to_arrow())
        # Arrow maps Delta strings to large_string in some reader versions.
        # Casting to the contract validates types and nullability separately.
        if [(f.name, str(f.type).replace('large_string', 'string'), f.nullable)
            for f in actual_schema] != [
                (f.name, str(f.type), f.nullable) for f in schema]:
            raise SnapshotConflict('Existing Delta schema differs from contract')
        current_rows = target.to_pyarrow_table(
            filters=[('ingestion_id', '=', ingestion_id)],
        ).cast(schema).to_pylist()
        current = _indexed(current_rows, TABLE_KEYS[table_name])
        for key, row in current.items():
            if key not in expected or expected[key] != row:
                raise SnapshotConflict('Existing immutable snapshot differs')
        missing = len(expected) - len(current)
        if missing:
            predicate = ' AND '.join(
                f'target.`{key}` = source.`{key}`' for key in TABLE_KEYS[table_name]
            )
            lease.assert_held()
            target.merge(
                source=rows, predicate=predicate,
                source_alias='source', target_alias='target',
                merge_schema=False,
                commit_properties=CommitProperties(max_commit_retries=0),
            ).when_not_matched_insert_all().execute()
            lease.assert_held()
            # A successful transaction alone does not prove key uniqueness.
            written = _indexed(target.to_pyarrow_table(
                filters=[('ingestion_id', '=', ingestion_id)],
            ).cast(schema).to_pylist(), TABLE_KEYS[table_name])
            if written != expected:
                raise SnapshotConflict('Committed snapshots failed verification')
        lease.assert_held()
        return TableCommit(self.table_path(table_name), target.version(), len(expected), missing)
