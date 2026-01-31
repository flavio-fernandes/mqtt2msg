#!/usr/bin/env python
import asyncio
import collections
from contextlib import AsyncExitStack

from aiomqtt import Client, MqttError

from mqtt2msg import const
from mqtt2msg import log
from mqtt2msg.config import Cfg
from mqtt2msg.jobs import (
    JobRefreshPublish,
    JobCheckExpirations,
    JobRunElection,
    JobMqttMsg,
)
from mqtt2msg.jobs import RunState
from mqtt2msg.topic_state import TopicState


async def periodic_timer_refresh_publish(events_q: asyncio.Queue):
    interval = Cfg().knobs.get(
        "periodic_interval_refresh_publish", const.PERIODIC_INTERVAL_REFRESH_PUBLISH
    )
    if interval == 0:
        logger.info("periodic_timer_refresh_publish is not needed")
    else:
        while True:
            await asyncio.sleep(interval)
            await events_q.put(JobRefreshPublish())


async def periodic_timer_expirations(events_q: asyncio.Queue):
    interval = (
        Cfg().knobs.get("periodic_interval_expirations")
        or const.PERIODIC_INTERVAL_EXPIRATIONS
    )
    while True:
        await asyncio.sleep(interval)
        await events_q.put(JobCheckExpirations())


async def periodic_timer_reelection(events_q: asyncio.Queue):
    interval = (
        Cfg().knobs.get("periodic_interval_reelection")
        or const.PERIODIC_INTERVAL_REELECTION
    )
    while True:
        await asyncio.sleep(interval)
        await events_q.put(JobRunElection())


async def handle_jobs(
    run_state: RunState, events_q: asyncio.Queue, mqtt_send_q: asyncio.Queue
):
    while True:
        job = await events_q.get()
        if not job.is_chatty:
            logger.debug(f"Handling: {job.name}")
        await job.handle_job(run_state, events_q, mqtt_send_q)
        events_q.task_done()


async def handle_mqtt_publish(client, send_q: asyncio.Queue):
    while True:
        topic, payload = await send_q.get()
        # logger.debug(f"Publishing: {topic} {payload}")
        try:
            await client.publish(topic, payload, timeout=15)
            logger.debug(f"Published: {topic} {payload}")
        except Exception as e:
            logger.error("client failed publish mqtt %s %s : %s", topic, payload, e)
        send_q.task_done()
        # Dampen publishes. This is a fail-safe and should not affect anything unless
        # there is a bug lurking somewhere
        await asyncio.sleep(1)


async def handle_mqtt_messages(message_iter, events_q: asyncio.Queue):
    try:
        async for message in message_iter:
            # aiomqtt Message.topic is a Topic object; string value is .value
            topic_obj = message.topic
            msg_topic = getattr(topic_obj, "value", str(topic_obj))

            payload = message.payload
            if isinstance(payload, (bytes, bytearray)):
                msg_payload = payload.decode(errors="replace")
            else:
                msg_payload = str(payload)

            logger.debug(f"Received mqtt topic:{msg_topic} payload:{msg_payload}")
            await events_q.put(JobMqttMsg(msg_topic, msg_payload))
    except MqttError as e:
        if not stop_gracefully:
            raise


async def cancel_tasks(tasks):
    logger.info("Cancelling all tasks")
    for task in tasks:
        if task.done():
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def main_loop():
    global stop_gracefully

    # https://pypi.org/project/asyncio-mqtt/
    logger.debug("Starting main event processing loop")
    cfg = Cfg()
    mqtt_broker_ip = cfg.mqtt_host or const.MQTT_DEFAULT_BROKER_IP
    mqtt_client_id = cfg.mqtt_client_id or const.MQTT_CLIENT_ID_DEFAULT
    mqtt_send_q = asyncio.Queue(maxsize=256)
    events_q = asyncio.Queue(maxsize=256)

    # We 💛 context managers. Let's create a stack to help
    # us manage them.
    async with AsyncExitStack() as stack:
        # Keep track of the asyncio tasks that we create, so that
        # we can cancel them on exit
        tasks = set()
        stack.push_async_callback(cancel_tasks, tasks)

        # Connect to the MQTT broker
        client = Client(mqtt_broker_ip, identifier=mqtt_client_id)
        await stack.enter_async_context(client)

        task = asyncio.create_task(handle_mqtt_messages(client.messages, events_q))
        tasks.add(task)

        run_state = RunState()
        for topic, attrs in cfg.topics.items():
            run_state.states[topic] = TopicState(topic, **attrs)
            await client.subscribe(topic)

        task = asyncio.create_task(handle_mqtt_publish(client, mqtt_send_q))
        tasks.add(task)

        task = asyncio.create_task(periodic_timer_refresh_publish(events_q))
        tasks.add(task)

        task = asyncio.create_task(periodic_timer_expirations(events_q))
        tasks.add(task)

        task = asyncio.create_task(periodic_timer_reelection(events_q))
        tasks.add(task)

        task = asyncio.create_task(handle_jobs(run_state, events_q, mqtt_send_q))
        tasks.add(task)

        # Wait for everything to complete (or fail due to, e.g., network errors)
        await asyncio.gather(*tasks)

    logger.debug("all done!")


# cfg_globals
stop_gracefully = False
logger = None


async def main():
    global stop_gracefully

    # Run the advanced_example indefinitely. Reconnect automatically
    # if the connection is lost.
    reconnect_interval = Cfg().reconnect_interval
    while not stop_gracefully:
        try:
            await main_loop()
        except MqttError as error:
            logger.warning(
                f'MQTT error "{error}". Reconnecting in {reconnect_interval} seconds.'
            )
        except (KeyboardInterrupt, SystemExit):
            logger.info("got KeyboardInterrupt")
            stop_gracefully = True
            break
        await asyncio.sleep(reconnect_interval)


if __name__ == "__main__":
    logger = log.getLogger()
    log.initLogger()

    knobs = Cfg().knobs
    if isinstance(knobs, collections.abc.Mapping):
        if knobs.get("log_to_console"):
            log.log_to_console()
        if knobs.get("log_level_debug"):
            log.set_log_level_debug()

    logger.debug("mqtt2msg process started")
    asyncio.run(main())
    if not stop_gracefully:
        raise RuntimeError("main is exiting")
