#!/usr/bin/env python

MQTT_CLIENT_ID_DEFAULT = "mqtt2msg"
MQTT_CLIENT_DEFAULT_TOPIC_MESSAGE = "/msg"
MQTT_DEFAULT_BROKER_IP = "192.168.10.238"
MQTT_DEFAULT_RECONNECT_INTERVAL = 13

DEFAULT_PRIORITY = 10  # the smaller, the more important
PERIODIC_INTERVAL_REFRESH_PUBLISH = 600  # how often to re-publish active (or empty)
PERIODIC_INTERVAL_EXPIRATIONS = 10  # how often to check for expirations
PERIODIC_INTERVAL_REELECTION = 5  # msgs with same priority will alternate based on this
