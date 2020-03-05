from snuba import settings
from snuba.query.query import Query
from snuba.query.query_processor import QueryProcessor
from snuba.request.request_settings import RequestSettings, SamplingMode


class SamplingRateProcessor(QueryProcessor):
    """
    Applies the appropriate sampling rate to the query to be executed.
    This will be a storage specific processor as soon as we will have a clickhouse
    specific query class and ast. As long as that does not exist, this
    is just applied at the very end of the query processing phase.

    Right now it does not much, but it will be the place where the adaptive sampling
    will be calculated and applied.
    """

    def process_query(self, query: Query, request_settings: RequestSettings,) -> None:
        if not query.get_data_source().supports_sample():
            query.set_sample(None)

        if request_settings.get_sampling_config().mode == SamplingMode.AUTO_DEPRECATED:
            if query.get_sample():
                return
            elif request_settings.get_turbo():
                query.set_sample(settings.TURBO_SAMPLE_RATE)

        # TODO: Support all the other sampling modes