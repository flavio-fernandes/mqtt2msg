#!/usr/bin/env python
from collections import namedtuple
from enum import Enum

import dateparser

from mqtt2msg import const
from mqtt2msg import log

logger = log.getLogger()


# https://python-3-patterns-idioms-test.readthedocs.io/en/latest/StateMachine.html
class StateMachine:
    def __init__(self, initialState):
        self.currentState = initialState

    def __str__(self):
        return self.currentState.name

    def handle_event(self, event):
        topic_next = self.currentState.next(event)
        if topic_next:
            logger.debug(
                f'state {self.currentState.name} event "{event}" ==> '
                f"{topic_next.next_state.name}"
            )
            self.currentState = States[topic_next.next_state]
            return topic_next.actions
        return []


# https://stackoverflow.com/questions/5221236/how-can-i-make-my-classes-usable-as-dict-keys
class TopicEvent:
    def __init__(self, event):
        self.event = event

    def __str__(self):
        return self.event

    def __eq__(self, other):
        return hasattr(other, "event") and self.event == other.event

    def __hash__(self):
        return hash(self.event)


# Static fields; an enumeration of instances:
TopicEvent.on = TopicEvent("topic on")
TopicEvent.off = TopicEvent("topic off")
TopicEvent.activated = TopicEvent("topic activated")
TopicEvent.deactivated = TopicEvent("topic deactivated")


class TopicAction(Enum):
    ENQUEUE = (10,)
    DEQUEUE = (20,)
    SET_EXPIRATION = (30,)
    PUBLISH = (40,)
    UNPUBLISH = (50,)


TopicNext = namedtuple("TopicNext", "actions next_state")


class State:
    def __init__(self, name):
        self.name = name

    def next(self, event):
        return self.NEXT.get(event)


class TopicStatesEnum(Enum):
    STATE_INACTIVE = (0,)
    STATE_QUEUED = (1,)
    STATE_ACTIVE = (2,)


class StateInactive(State):
    def __init__(self):
        super().__init__(TopicStatesEnum.STATE_INACTIVE.name)

    NEXT = {
        TopicEvent.on: TopicNext(
            [TopicAction.ENQUEUE, TopicAction.SET_EXPIRATION],
            TopicStatesEnum.STATE_QUEUED,
        ),
    }


class StateQueued(State):
    def __init__(self):
        super().__init__(TopicStatesEnum.STATE_QUEUED.name)

    NEXT = {
        TopicEvent.on: TopicNext(
            [TopicAction.SET_EXPIRATION], TopicStatesEnum.STATE_QUEUED
        ),
        TopicEvent.off: TopicNext(
            [TopicAction.DEQUEUE], TopicStatesEnum.STATE_INACTIVE
        ),
        TopicEvent.activated: TopicNext(
            [TopicAction.PUBLISH], TopicStatesEnum.STATE_ACTIVE
        ),
    }


class StateActive(State):
    def __init__(self):
        super().__init__(TopicStatesEnum.STATE_ACTIVE.name)

    NEXT = {
        TopicEvent.on: TopicNext(
            [TopicAction.SET_EXPIRATION, TopicAction.PUBLISH],
            TopicStatesEnum.STATE_ACTIVE,
        ),
        TopicEvent.off: TopicNext(
            [TopicAction.DEQUEUE], TopicStatesEnum.STATE_INACTIVE
        ),
        TopicEvent.deactivated: TopicNext(
            [TopicAction.UNPUBLISH], TopicStatesEnum.STATE_QUEUED
        ),
    }


States = {
    TopicStatesEnum.STATE_INACTIVE: StateInactive(),
    TopicStatesEnum.STATE_QUEUED: StateQueued(),
    TopicStatesEnum.STATE_ACTIVE: StateActive(),
}

TimeConstraint = namedtuple("TimeConstraint", "hour minute second")

TimeConstraintRange = namedtuple("TimeConstraintRange", "start stop")


class TopicState(StateMachine):
    def __init__(
        self,
        topic,
        msg=None,
        msg_topic=None,
        priority=const.DEFAULT_PRIORITY,
        expiration=None,
        on=None,
        off=None,
        on_high_water=None,
        off_low_water=None,
        on_enabled_after=None,
        on_enabled_before=None,
        on_enabled_between=None,
        on_disabled_after=None,
        on_disabled_before=None,
        on_disabled_between=None,
    ):
        # Initial state
        super().__init__(States[TopicStatesEnum.STATE_INACTIVE])
        self.topic = topic
        self.msg = msg or topic
        self.msg_topic = msg_topic
        self.priority = int(priority)
        self.expiration = int(expiration) if expiration else None
        self.on = self._parse_on_off(on, "on")
        self.off = self._parse_on_off(off, "off")
        self.on_high_water = float(on_high_water) if on_high_water is not None else None
        self.off_low_water = float(off_low_water) if off_low_water is not None else None
        self.on_enabled_after = self._parse_date(topic, on_enabled_after)
        self.on_enabled_before = self._parse_date(topic, on_enabled_before)
        self.on_enabled_between = self._parse_date_range(topic, on_enabled_between)
        self.on_disabled_after = self._parse_date(topic, on_disabled_after)
        self.on_disabled_before = self._parse_date(topic, on_disabled_before)
        self.on_disabled_between = self._parse_date_range(topic, on_disabled_between)

        # Sanity checks
        if not self.topic:
            raise RuntimeError("topic attribute provided is not okay")
        if not any((self.on, self.on_high_water)):
            raise RuntimeError(f"{topic}: not turning on")
        if not any((self.off, self.off_low_water, self.expiration)):
            raise RuntimeError(f"{topic}: not turning off")
        if self.on and self.on.intersection(self.off):
            raise RuntimeError(f"{topic}: on and off should not have common value(s)")

        self.next_expiration = None
        logger.debug(f"Init state for {topic} with attrs {self.__dict__}")

    @staticmethod
    def _parse_on_off(value, default_value):
        if value is None:
            return frozenset({default_value})
        if not value:
            return frozenset({})
        if isinstance(value, list):
            return frozenset(value)
        return frozenset({value})

    @staticmethod
    def _parse_date(topic, value):
        if not value:
            return None
        parsed_value = dateparser.parse(value)
        if not parsed_value:
            # Try giving it a TZ to make it happy?
            # https://dateparser.readthedocs.io/en/latest/settings.html
            parsed_value = dateparser.parse(value, settings={"TIMEZONE": "UTC"})
        if not parsed_value:
            raise RuntimeError(f"{topic}: sad panda while trying to parse {value}")
        return TimeConstraint(
            parsed_value.hour, parsed_value.minute, parsed_value.second
        )

    @classmethod
    def _parse_date_range(cls, topic, value):
        if not value:
            return None
        start, stop = value.split(",")
        return TimeConstraintRange(
            cls._parse_date(topic, start.strip()), cls._parse_date(topic, stop.strip())
        )
