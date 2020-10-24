from typing import Sequence

from snuba.clickhouse.columns import Column, DateTime, UInt
from snuba.clusters.storage_sets import StorageSetKey
from snuba.migrations import migration, operations, table_engines
from snuba.migrations.columns import MigrationModifiers, nullable

columns: Sequence[Column[MigrationModifiers]] = [
    # Kafka topic offset
    Column("offset", UInt(64)),
    # GroupStatus in Sentry does not have a 'DELETED' state that reflects the deletion
    # of the record. Having a dedicated clickhouse-only flag to identify this case seems
    # more consistent than add an additional value into the status field below that does not
    # exists on the Sentry side.
    Column("record_deleted", UInt(8)),
    # PG columns
    Column("project_id", UInt(64)),
    Column("id", UInt(64)),
    Column("status", UInt(8, nullable())),
    Column("last_seen", DateTime(nullable())),
    Column("first_seen", DateTime(nullable())),
    Column("active_at", DateTime(nullable())),
    Column("first_release_id", UInt(64, nullable())),
]


class Migration(migration.MultiStepMigration):
    blocking = False

    def forwards_local(self) -> Sequence[operations.Operation]:
        return [
            operations.CreateTable(
                storage_set=StorageSetKey.EVENTS,
                table_name="groupedmessage_local",
                columns=columns,
                engine=table_engines.ReplacingMergeTree(
                    storage_set=StorageSetKey.EVENTS,
                    version_column="offset",
                    order_by="(project_id, id)",
                    sample_by="id",
                    unsharded=True,
                ),
            )
        ]

    def backwards_local(self) -> Sequence[operations.Operation]:
        return [
            operations.DropTable(
                storage_set=StorageSetKey.EVENTS, table_name="groupedmessage_local",
            )
        ]

    def forwards_dist(self) -> Sequence[operations.Operation]:
        return [
            operations.CreateTable(
                storage_set=StorageSetKey.EVENTS,
                table_name="groupedmessage_dist",
                columns=columns,
                engine=table_engines.Distributed(
                    local_table_name="groupedmessage_local", sharding_key=None,
                ),
            )
        ]

    def backwards_dist(self) -> Sequence[operations.Operation]:
        return [
            operations.DropTable(
                storage_set=StorageSetKey.EVENTS, table_name="groupedmessage_dist",
            )
        ]
