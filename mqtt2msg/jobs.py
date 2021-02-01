#!/usr/bin/env python

import asyncio
import collections
import datetime as dt
import heapq
import json
import operator
import random
from abc import ABC, abstractmethod
from typing import Optional

from mqtt2msg import log
from mqtt2msg.config import Cfg
from mqtt2msg.topic_state import (
    TimeConstraint,
    TimeConstraintRange,
    TopicAction,
    TopicEvent,
    TopicState,
)

logger = log.getLogger()

QPrio = collections.namedtuple("QPrio", "priority topic")


class RunState:
    def __init__(self):
        self.states: dict[str, TopicState] = {}
        self.enqueued: list[QPrio] = []  # heapq of QPrio
        self.activated = None


class Job(ABC):
    def __init__(self, topic: Optional[str] = None):
        self.topic = topic if topic else "<n/a topic>"

    @property
    def is_chatty(self):
        return False

    @property
    def name(self):
        return f"{self.__class__.__name__} for {self.topic}"

    @abstractmethod
    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        pass


class JobRefreshPublish(Job):
    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        if run_state.activated:
            topic_state = run_state.states.get(run_state.activated)
            assert topic_state  # if its activated, it better be in run_state
            send_mqtt_topic = topic_state.msg_topic or Cfg().mqtt_message_topic
            send_mqtt_payload = topic_state.msg
            also_clear_cfg_topic = topic_state.msg_topic
        else:
            send_mqtt_topic = Cfg().mqtt_message_topic
            send_mqtt_payload = None
            also_clear_cfg_topic = False
        logger.info(f"Refreshing {send_mqtt_topic} with '{send_mqtt_payload}'.")
        await mqtt_send_q.put((send_mqtt_topic, send_mqtt_payload))
        if also_clear_cfg_topic:
            await mqtt_send_q.put((Cfg().mqtt_message_topic, ""))


class JobCheckExpirations(Job):
    @property
    def is_chatty(self):
        return True

    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        if not run_state.activated:
            return  # noop: nothing active means nothing to expire
        now = dt.datetime.now()
        for qprio in run_state.enqueued:
            topic_state = run_state.states.get(qprio.topic)
            if not topic_state or not topic_state.next_expiration:
                continue
            if now <= topic_state.next_expiration:
                continue
            if run_state.activated == qprio.topic:
                await events_q.put(JobStmActionUnpublish(run_state.activated))
            # Note: Expiration is the same as an 'off' event
            for action in topic_state.handle_event(TopicEvent.off):
                await events_q.put(_alloc_job(action, qprio.topic))
            logger.info(f"Expired {qprio.topic}")


class JobRunElection(Job):
    @property
    def is_chatty(self):
        return True

    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):

        if not run_state.enqueued:
            if not run_state.activated:
                return  # noop: nothing active and remains that way

            logger.debug(f"Deactivated all topics (last one was {run_state.activated})")
            await events_q.put(JobStmActionUnpublish(run_state.activated))
            run_state.activated = None
            return

        # Look at all entries in run_state.enqueued and pick a winner
        best_priority = run_state.enqueued[0].priority
        candidates = [
            x.topic
            for x in heapq.nsmallest(len(run_state.enqueued), run_state.enqueued)
            if x.priority == best_priority and x.topic != run_state.activated
        ]  # skip current king of the hill
        if not candidates:
            return  # noop: nothing better than what we currently have

        # Demote current king
        if run_state.activated:
            topic_state = run_state.states.get(run_state.activated)
            assert topic_state  # if its activated, it better be in run_state
            for action in topic_state.handle_event(TopicEvent.deactivated):
                await events_q.put(_alloc_job(action, run_state.activated))
            logger.debug(f"Demoted {run_state.activated})")

        # Promote new king
        new_best_topic = random.choice(candidates)
        topic_state = run_state.states.get(new_best_topic)
        assert topic_state  # if its queued, it better be in run_state
        for action in topic_state.handle_event(TopicEvent.activated):
            await events_q.put(_alloc_job(action, new_best_topic))

        run_state.activated = new_best_topic
        logger.debug(f"Promoted {run_state.activated})")


class JobMqttMsg(Job):
    def __init__(self, topic, payload):
        super().__init__(topic)
        self.payload = payload

    @classmethod
    def _ts_from_time_constraint_range(
        cls, now: dt.datetime, time_constraint_range: Optional[TimeConstraintRange]
    ) -> Optional[tuple]:
        if not time_constraint_range:
            return None
        return (
            cls._ts_from_time_constraint(now, time_constraint_range.start),
            cls._ts_from_time_constraint(now, time_constraint_range.stop),
        )

    @classmethod
    def _ts_from_time_constraint(
        cls, now: dt.datetime, time_constraint: Optional[TimeConstraint]
    ) -> Optional[dt.datetime]:
        return (
            now.replace(
                hour=time_constraint.hour,
                minute=time_constraint.minute,
                second=time_constraint.second,
                microsecond=0,
            )
            if time_constraint
            else None
        )

    @staticmethod
    def _time_restricted(
        now: dt.datetime, oper: operator, ts: Optional[dt.datetime], is_disable: bool
    ) -> bool:
        # Goal: return true if and only if we want to ignore the 'on' event.
        if not ts:
            return False  # not restricted
        is_restricted = oper(now, ts)
        return is_restricted if is_disable else not is_restricted

    @staticmethod
    def _time_restricted_range(
        now: dt.datetime, tss: Optional[tuple], is_disable: bool
    ) -> bool:
        # Goal: return true if and only if we want to ignore the 'on' event.
        if not tss:
            return False  # not restricted
        is_restricted = now >= tss[0] and now <= tss[1]
        return is_restricted if is_disable else not is_restricted

    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        topic_state = run_state.states.get(self.topic)
        if not topic_state:
            return  # noop: topic has no corresponding stm

        try:
            payload_number = float(self.payload)
        except ValueError:
            payload_number = None

        # map payload of mqtt message to a state machine event
        if self.payload in topic_state.on:
            stm_event = TopicEvent.on
        elif (
            payload_number is not None
            and topic_state.on_high_water is not None
            and payload_number >= topic_state.on_high_water
        ):
            stm_event = TopicEvent.on
        elif self.payload in topic_state.off:
            stm_event = TopicEvent.off
        elif (
            payload_number is not None
            and topic_state.off_low_water is not None
            and payload_number < topic_state.off_low_water
        ):
            stm_event = TopicEvent.off
        else:
            return

        if stm_event == TopicEvent.on and any(
            (
                topic_state.on_enabled_before,
                topic_state.on_enabled_after,
                topic_state.on_enabled_between,
                topic_state.on_disabled_before,
                topic_state.on_disabled_after,
                topic_state.on_disabled_between,
            )
        ):
            now = dt.datetime.now()
            enabled_before = self._ts_from_time_constraint(
                now, topic_state.on_enabled_before
            )
            enabled_after = self._ts_from_time_constraint(
                now, topic_state.on_enabled_after
            )
            enabled_between = self._ts_from_time_constraint_range(
                now, topic_state.on_enabled_between
            )
            disabled_before = self._ts_from_time_constraint(
                now, topic_state.on_disabled_before
            )
            disabled_after = self._ts_from_time_constraint(
                now, topic_state.on_disabled_after
            )
            disabled_between = self._ts_from_time_constraint_range(
                now, topic_state.on_disabled_between
            )
            if any(
                (
                    self._time_restricted(
                        now, operator.lt, enabled_before, is_disable=False
                    ),
                    self._time_restricted(
                        now, operator.gt, enabled_after, is_disable=False
                    ),
                    self._time_restricted_range(now, enabled_between, is_disable=False),
                    self._time_restricted(
                        now, operator.le, disabled_before, is_disable=True
                    ),
                    self._time_restricted(
                        now, operator.ge, disabled_after, is_disable=True
                    ),
                    self._time_restricted_range(now, disabled_between, is_disable=True),
                )
            ):
                logger.info(
                    f"Ignoring _on_ event for {self.topic} due to time restriction"
                )
                return

        for action in topic_state.handle_event(stm_event):
            await events_q.put(_alloc_job(action, self.topic))


class JobStmAction(Job):
    pass


class JobStmActionEnqueue(JobStmAction):
    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        topic_state = run_state.states.get(self.topic)
        if not topic_state:
            return  # noop: topic has no corresponding stm

        send_mqtt_topic = f"{Cfg().mqtt_message_topic}/start"
        mqtt_payload = json.dumps({"topic": self.topic, "msg": topic_state.msg})
        await mqtt_send_q.put((send_mqtt_topic, mqtt_payload))

        heapq.heappush(run_state.enqueued, QPrio(topic_state.priority, self.topic))
        if run_state.enqueued[0].priority == topic_state.priority:
            # If we get here, the topic_state we just queued is candidate as the one
            # to be published. Let's go ahead and queue up an event for taking care of it
            logger.info(f"Enqueued {self.topic} is possibly the one to be activated")
            await events_q.put(JobRunElection())


class JobStmActionDequeue(JobStmAction):
    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        topic_state = run_state.states.get(self.topic)
        if not topic_state:
            return  # noop: topic has no corresponding stm

        logger.debug(f"Auto-clearing expiration for {self.topic}")
        topic_state.next_expiration = None

        send_mqtt_topic = f"{Cfg().mqtt_message_topic}/stop"
        mqtt_payload = json.dumps({"topic": self.topic, "msg": topic_state.msg})
        await mqtt_send_q.put((send_mqtt_topic, mqtt_payload))

        # Demote current king
        if self.topic == run_state.activated:
            logger.info(f"Dequeued {self.topic} was the active entry")
            await events_q.put(JobRunElection())

        try:
            run_state.enqueued.remove(QPrio(topic_state.priority, self.topic))
            heapq.heapify(run_state.enqueued)  # needed because list is no longer heap
        except ValueError:
            logger.debug(
                f"Dequeued {self.topic} was already removed from run_state.enqueued"
            )
        if not run_state.enqueued:
            logger.info("Dequeued all entries")


class JobStmActionSetExpiration(JobStmAction):
    async def handle_job(
        self, run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        topic_state = run_state.states.get(self.topic)
        if not topic_state:
            return  # noop: topic has no corresponding stm
        if topic_state.expiration:
            topic_state.next_expiration = dt.datetime.now() + dt.timedelta(
                seconds=topic_state.expiration
            )
            logger.info(
                f"Setting expiration for {self.topic} to "
                f"{topic_state.next_expiration}"
            )


class JobStmActionPublish(JobStmAction):
    async def handle_job(
        self, run_state: RunState, _events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        topic_state = run_state.states.get(self.topic)
        if not topic_state:
            return  # noop: topic has no corresponding stm
        send_mqtt_topic = topic_state.msg_topic or Cfg().mqtt_message_topic
        logger.info(
            f"Publishing {self.topic} as current_active_topic. "
            f"mqtt topic: {send_mqtt_topic}"
        )
        await mqtt_send_q.put((send_mqtt_topic, topic_state.msg))


class JobStmActionUnpublish(JobStmAction):
    async def handle_job(
        self, run_state: RunState, _events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
    ):
        topic_state = run_state.states.get(self.topic)
        if not topic_state:
            return  # noop: topic has no corresponding stm
        send_mqtt_topic = topic_state.msg_topic or Cfg().mqtt_message_topic
        logger.info(
            f"Unpublishing {self.topic} as current_active_topic. "
            f"mqtt topic: {send_mqtt_topic}"
        )
        await mqtt_send_q.put((send_mqtt_topic, None))


def _alloc_job(stm_action: TopicAction, topic: str) -> JobStmAction:
    if stm_action == TopicAction.ENQUEUE:
        return JobStmActionEnqueue(topic)
    if stm_action == TopicAction.DEQUEUE:
        return JobStmActionDequeue(topic)
    if stm_action == TopicAction.SET_EXPIRATION:
        return JobStmActionSetExpiration(topic)
    if stm_action == TopicAction.PUBLISH:
        return JobStmActionPublish(topic)
    assert stm_action == TopicAction.UNPUBLISH
    return JobStmActionUnpublish(topic)
