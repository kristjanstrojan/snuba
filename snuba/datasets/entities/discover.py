from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, Optional, Sequence, Set

from snuba.clickhouse.columns import (
    UUID,
    Array,
    ColumnSet,
    DateTime,
    FixedString,
    Float,
    Nested,
    Nullable,
    String,
    UInt,
)
from snuba.clickhouse.translators.snuba import SnubaClickhouseStrictTranslator
from snuba.clickhouse.translators.snuba.allowed import (
    ColumnMapper,
    CurriedFunctionCallMapper,
    FunctionCallMapper,
    SubscriptableReferenceMapper,
)
from snuba.clickhouse.translators.snuba.mappers import (
    ColumnToLiteral,
    ColumnToMapping,
    SubscriptableMapper,
)
from snuba.clickhouse.translators.snuba.mapping import TranslationMappers
from snuba.datasets.entity import Entity
from snuba.datasets.plans.single_storage import SelectedStorageQueryPlanBuilder
from snuba.datasets.storages import StorageKey
from snuba.datasets.storages.factory import get_storage
from snuba.datasets.entities.events import EventsEntity, EventsQueryStorageSelector
from snuba.datasets.entities.transactions import TransactionsEntity
from snuba.query.dsl import identity
from snuba.query.expressions import (
    Column,
    CurriedFunctionCall,
    FunctionCall,
    Literal,
    SubscriptableReference,
)
from snuba.query.extensions import QueryExtension
from snuba.query.matchers import FunctionCall as FunctionCallMatch
from snuba.query.matchers import Literal as LiteralMatch
from snuba.query.matchers import Or
from snuba.query.matchers import String as StringMatch
from snuba.query.processors import QueryProcessor
from snuba.query.processors.basic_functions import BasicFunctionsProcessor
from snuba.query.processors.tags_expander import TagsExpanderProcessor
from snuba.query.processors.timeseries_processor import TimeSeriesProcessor
from snuba.query.project_extension import ProjectExtension
from snuba.query.timeseries_extension import TimeSeriesExtension
from snuba.util import qualified_column


@dataclass(frozen=True)
class DefaultNoneColumnMapper(ColumnMapper):
    """
    This maps a list of column names to None (NULL in SQL) as it is done
    in the discover column_expr method today. It should not be used for
    any other reason or use case, thus it should not be moved out of
    the discover dataset file.
    """

    columns: ColumnSet

    def attempt_map(
        self, expression: Column, children_translator: SnubaClickhouseStrictTranslator,
    ) -> Optional[Literal]:
        if expression.column_name in self.columns:
            return Literal(
                alias=expression.alias
                or qualified_column(
                    expression.column_name, expression.table_name or ""
                ),
                value=None,
            )
        else:
            return None


@dataclass
class DefaultNoneFunctionMapper(FunctionCallMapper):
    """
    Maps the list of function names to NULL.
    """

    function_names: Set[str]

    def __post_init__(self) -> None:
        self.function_match = FunctionCallMatch(
            Or([StringMatch(func) for func in self.function_names])
        )

    def attempt_map(
        self,
        expression: FunctionCall,
        children_translator: SnubaClickhouseStrictTranslator,
    ) -> Optional[FunctionCall]:
        if self.function_match.match(expression):
            return identity(Literal(None, None), expression.alias)

        return None


@dataclass(frozen=True)
class DefaultIfNullFunctionMapper(FunctionCallMapper):
    """
    If a function is being called on a column that doesn't exist, or is being
    called on NULL, change the entire function to be NULL.
    """

    function_match = FunctionCallMatch(
        StringMatch("ifNull"), (LiteralMatch(), LiteralMatch())
    )

    def attempt_map(
        self,
        expression: FunctionCall,
        children_translator: SnubaClickhouseStrictTranslator,
    ) -> Optional[FunctionCall]:
        parameters = tuple(p.accept(children_translator) for p in expression.parameters)
        all_null = True
        for param in parameters:
            # Handle wrapped functions that have been converted to ifNull(NULL, NULL)
            fmatch = self.function_match.match(param)
            if fmatch is None:
                if isinstance(param, Literal):
                    if param.value is not None:
                        all_null = False
                        break
                else:
                    all_null = False
                    break

        if all_null and len(parameters) > 0:
            # Currently function mappers require returning other functions. So return this
            # to keep the mapper happy.
            return FunctionCall(
                expression.alias, "ifNull", (Literal(None, None), Literal(None, None))
            )

        return None


@dataclass(frozen=True)
class DefaultIfNullCurriedFunctionMapper(CurriedFunctionCallMapper):
    """
    If a curried function is being called on a column that doesn't exist, or is being
    called on NULL, change the entire function to be NULL.
    """

    function_match = FunctionCallMatch(
        StringMatch("ifNull"), (LiteralMatch(), LiteralMatch())
    )

    def attempt_map(
        self,
        expression: CurriedFunctionCall,
        children_translator: SnubaClickhouseStrictTranslator,
    ) -> Optional[CurriedFunctionCall]:
        internal_function = expression.internal_function.accept(children_translator)
        assert isinstance(internal_function, FunctionCall)  # mypy
        parameters = tuple(p.accept(children_translator) for p in expression.parameters)

        all_null = True
        for param in parameters:
            # Handle wrapped functions that have been converted to ifNull(NULL, NULL)
            fmatch = self.function_match.match(param)
            if fmatch is None:
                if isinstance(param, Literal):
                    if param.value is not None:
                        all_null = False
                        break
                else:
                    all_null = False
                    break

        if all_null and len(parameters) > 0:
            # Currently curried function mappers require returning other curried functions.
            # So return this to keep the mapper happy.
            return CurriedFunctionCall(
                alias=expression.alias,
                internal_function=FunctionCall(
                    None,
                    f"{internal_function.function_name}OrNull",
                    internal_function.parameters,
                ),
                parameters=tuple(Literal(None, None) for p in parameters),
            )

        return None


@dataclass(frozen=True)
class DefaultNoneSubscriptMapper(SubscriptableReferenceMapper):
    """
    This maps a subscriptable reference to None (NULL in SQL) as it is done
    in the discover column_expr method today. It should not be used for
    any other reason or use case, thus it should not be moved out of
    the discover dataset file.
    """

    subscript_names: Set[str]

    def attempt_map(
        self,
        expression: SubscriptableReference,
        children_translator: SnubaClickhouseStrictTranslator,
    ) -> Optional[Literal]:
        if expression.column.column_name in self.subscript_names:
            return Literal(alias=expression.alias, value=None)
        else:
            return None


EVENTS_COLUMNS = ColumnSet(
    [
        ("group_id", UInt(64, [Nullable()])),
        ("primary_hash", FixedString(32, [Nullable()])),
        # Promoted tags
        ("level", String([Nullable()])),
        ("logger", String([Nullable()])),
        ("server_name", String([Nullable()])),
        ("site", String([Nullable()])),
        ("url", String([Nullable()])),
        ("location", String([Nullable()])),
        ("culprit", String([Nullable()])),
        ("received", DateTime([Nullable()])),
        ("sdk_integrations", Array(String(), [Nullable()])),
        ("version", String([Nullable()])),
        # exception interface
        (
            "exception_stacks",
            Nested(
                [
                    ("type", String([Nullable()])),
                    ("value", String([Nullable()])),
                    ("mechanism_type", String([Nullable()])),
                    ("mechanism_handled", UInt(8, [Nullable()])),
                ]
            ),
        ),
        (
            "exception_frames",
            Nested(
                [
                    ("abs_path", String([Nullable()])),
                    ("filename", String([Nullable()])),
                    ("package", String([Nullable()])),
                    ("module", String([Nullable()])),
                    ("function", String([Nullable()])),
                    ("in_app", UInt(8, [Nullable()])),
                    ("colno", UInt(32, [Nullable()])),
                    ("lineno", UInt(32, [Nullable()])),
                    ("stack_level", UInt(16)),
                ]
            ),
        ),
        ("modules", Nested([("name", String()), ("version", String())])),
    ]
)

TRANSACTIONS_COLUMNS = ColumnSet(
    [
        ("trace_id", UUID([Nullable()])),
        ("span_id", UInt(64, [Nullable()])),
        ("transaction_hash", UInt(64, [Nullable()])),
        ("transaction_op", String([Nullable()])),
        ("transaction_status", UInt(8, [Nullable()])),
        ("duration", UInt(32, [Nullable()])),
        ("measurements", Nested([("key", String()), ("value", Float(64))]),),
    ]
)


events_translation_mappers = TranslationMappers(
    columns=[DefaultNoneColumnMapper(TRANSACTIONS_COLUMNS)],
    functions=[DefaultNoneFunctionMapper({"apdex", "failure_rate"})],
    subscriptables=[DefaultNoneSubscriptMapper({"measurements"})],
)

transaction_translation_mappers = TranslationMappers(
    columns=[
        ColumnToLiteral(None, "group_id", 0),
        DefaultNoneColumnMapper(EVENTS_COLUMNS),
    ],
    functions=[DefaultNoneFunctionMapper({"isHandled", "notHandled"})],
)

null_function_translation_mappers = TranslationMappers(
    curried_functions=[DefaultIfNullCurriedFunctionMapper()],
    functions=[DefaultIfNullFunctionMapper()],
)


class DiscoverEntity(Entity):
    """
    Entity that represents both errors and transactions. This is currently backed
    by the events storage but will eventually be switched to use use the merge table storage.
    """

    def __init__(self) -> None:
        self.__common_columns = ColumnSet(
            [
                ("event_id", FixedString(32)),
                ("project_id", UInt(64)),
                ("type", String([Nullable()])),
                ("timestamp", DateTime()),
                ("platform", String([Nullable()])),
                ("environment", String([Nullable()])),
                ("release", String([Nullable()])),
                ("dist", String([Nullable()])),
                ("user", String([Nullable()])),
                ("transaction", String([Nullable()])),
                ("message", String([Nullable()])),
                ("title", String([Nullable()])),
                # User
                ("user_id", String([Nullable()])),
                ("username", String([Nullable()])),
                ("email", String([Nullable()])),
                ("ip_address", String([Nullable()])),
                # SDK
                ("sdk_name", String([Nullable()])),
                ("sdk_version", String([Nullable()])),
                # geo location context
                ("geo_country_code", String([Nullable()])),
                ("geo_region", String([Nullable()])),
                ("geo_city", String([Nullable()])),
                ("http_method", String([Nullable()])),
                ("http_referer", String([Nullable()])),
                # Other tags and context
                ("tags", Nested([("key", String()), ("value", String())])),
                ("contexts", Nested([("key", String()), ("value", String())])),
            ]
        )
        self.__events_columns = EVENTS_COLUMNS
        self.__transactions_columns = TRANSACTIONS_COLUMNS

        events_storage = get_storage(StorageKey.EVENTS)

        super().__init__(
            storages=[events_storage],
            query_plan_builder=SelectedStorageQueryPlanBuilder(
                selector=EventsQueryStorageSelector(
                    mappers=events_translation_mappers.concat(
                        transaction_translation_mappers
                    )
                    .concat(null_function_translation_mappers)
                    .concat(
                        TranslationMappers(
                            # XXX: Remove once we are using errors
                            columns=[
                                ColumnToMapping(
                                    None, "release", None, "tags", "sentry:release"
                                ),
                                ColumnToMapping(
                                    None, "dist", None, "tags", "sentry:dist"
                                ),
                                ColumnToMapping(
                                    None, "user", None, "tags", "sentry:user"
                                ),
                            ],
                            subscriptables=[
                                SubscriptableMapper(None, "tags", None, "tags"),
                                SubscriptableMapper(None, "contexts", None, "contexts"),
                            ],
                        )
                    )
                )
            ),
            abstract_column_set=(
                self.__common_columns
                + self.__events_columns
                + self.__transactions_columns
            ),
            writable_storage=None,
        )

    def get_query_processors(self) -> Sequence[QueryProcessor]:
        return [
            TimeSeriesProcessor({"time": "timestamp"}, ("timestamp",)),
            TagsExpanderProcessor(),
            BasicFunctionsProcessor(),
        ]

    def get_extensions(self) -> Mapping[str, QueryExtension]:
        return {
            "project": ProjectExtension(project_column="project_id"),
            "timeseries": TimeSeriesExtension(
                default_granularity=3600,
                default_window=timedelta(days=5),
                timestamp_column="timestamp",
            ),
        }


class DiscoverEventsEntity(EventsEntity):
    """
    Identical to EventsEntity except it maps columns and functions present in the
    transactions entity to null. This logic will eventually move to Sentry and this
    entity can be deleted and replaced with the EventsEntity directly.
    """

    def __init__(self) -> None:
        super().__init__(
            custom_mappers=events_translation_mappers.concat(
                null_function_translation_mappers
            )
        )


class DiscoverTransactionsEntity(TransactionsEntity):
    """
    Identical to TransactionsEntity except it maps columns and functions present
    in the events entity to null. This logic will eventually move to Sentry and this
    entity can be deleted and replaced with the TransactionsEntity directly.
    """

    def __init__(self) -> None:
        super().__init__(
            custom_mappers=transaction_translation_mappers.concat(
                null_function_translation_mappers
            )
        )
