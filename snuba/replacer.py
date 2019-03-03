import logging
import six
import time
from datetime import datetime
import simplejson as json

from batching_kafka_consumer import AbstractBatchWorker

from . import settings
from snuba.processor import InvalidMessageType, InvalidMessageVersion
from snuba.redis import redis_client


logger = logging.getLogger('snuba.replacer')


# TODO should this be in clickhouse.py
CLICKHOUSE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

EXCLUDE_GROUPS = object()
NEEDS_FINAL = object()


# TODO the whole needs_final calculation here is also specific to the events dataset
# specifically the reliance on a list of groups.
def get_project_exclude_groups_key(project_id):
    return "project_exclude_groups:%s" % project_id


def set_project_exclude_groups(project_id, group_ids):
    """Add {group_id: now, ...} to the ZSET for each `group_id` to exclude,
    remove outdated entries based on `settings.REPLACER_KEY_TTL`, and expire
    the entire ZSET incase it's rarely touched."""

    now = time.time()
    key = get_project_exclude_groups_key(project_id)
    p = redis_client.pipeline()

    p.zadd(key, **{str(group_id): now for group_id in group_ids})
    p.zremrangebyscore(key, -1, now - settings.REPLACER_KEY_TTL)
    p.expire(key, int(settings.REPLACER_KEY_TTL))

    p.execute()


def get_project_needs_final_key(project_id):
    return "project_needs_final:%s" % project_id


def set_project_needs_final(project_id):
    return redis_client.set(
        get_project_needs_final_key(project_id), True, ex=settings.REPLACER_KEY_TTL
    )


def get_projects_query_flags(project_ids):
    """\
    1. Fetch `needs_final` for each Project
    2. Fetch groups to exclude for each Project
    3. Trim groups to exclude ZSET for each Project

    Returns (needs_final, group_ids_to_exclude)
    """

    project_ids = set(project_ids)
    now = time.time()
    p = redis_client.pipeline()

    needs_final_keys = [get_project_needs_final_key(project_id) for project_id in project_ids]
    for needs_final_key in needs_final_keys:
        p.get(needs_final_key)

    exclude_groups_keys = [get_project_exclude_groups_key(project_id) for project_id in project_ids]
    for exclude_groups_key in exclude_groups_keys:
        p.zremrangebyscore(exclude_groups_key, float('-inf'), now - settings.REPLACER_KEY_TTL)
        p.zrevrangebyscore(exclude_groups_key, float('inf'), now - settings.REPLACER_KEY_TTL)

    results = p.execute()

    needs_final = any(results[:len(project_ids)])
    exclude_groups = sorted({
        int(group_id) for group_id
        in sum(results[(len(project_ids) + 1)::2], [])
    })

    return (needs_final, exclude_groups)


class ReplacerWorker(AbstractBatchWorker):
    """
    A consumer/worker that processes replacements for the events dataset.

    A replacement is a message in kafka describing an action that mutates snuba
    data. We process this action message into replacement event row(s) with new
    values for some columns. These are inserted into Clickhouse and will replace
    the existing rows with the same primary key upon the next OPTIMIZE.
    """
    def __init__(self, clickhouse, dataset, metrics=None):
        self.clickhouse = clickhouse
        self.dataset = dataset
        self.metrics = metrics

    def process_message(self, message):
        message = json.loads(message.value())
        version = message[0]

        if version == 2:
            type_, event = message[1:3]

            # TODO, to make this properly generic, the processor
            # should probabluy deal with all this.
            if type_ in ('start_delete_groups', 'start_merge', 'start_unmerge', 'start_delete_tag'):
                return None
            elif type_ in ('end_merge', 'end_delete_tag' ,'end_unmerge', 'end_delete_groups'):
                return self.dataset.PROCESSOR.process_replacement(type_, event)
            else:
                raise InvalidMessageType("Invalid message type: {}".format(type_))
        else:
            raise InvalidMessageVersion("Unknown message format: " + str(message))

    def flush_batch(self, batch):
        for count_query_template, insert_query_template, query_args, query_time_flags in batch:
            # TODO processor should probably insert table name
            query_args.update({'table_name': self.dataset.SCHEMA.QUERY_TABLE})
            count = self.clickhouse.execute_robust(count_query_template % query_args)[0][0]
            if count == 0:
                continue

            # query_time_flags == (type, project_id, [...data...])
            flag_type, project_id = query_time_flags[:2]
            if flag_type == NEEDS_FINAL:
                set_project_needs_final(project_id)
            elif flag_type == EXCLUDE_GROUPS:
                group_ids = query_time_flags[2]
                set_project_exclude_groups(project_id, group_ids)

            t = time.time()
            logger.debug("Executing replace query: %s" % (insert_query_template % query_args))
            self.clickhouse.execute_robust(insert_query_template % query_args)
            duration = int((time.time() - t) * 1000)
            logger.info("Replacing %s rows took %sms" % (count, duration))
            if self.metrics:
                self.metrics.timing('replacements.count', count)
                self.metrics.timing('replacements.duration', duration)

    def shutdown(self):
        pass
