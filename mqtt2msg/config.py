#!/usr/bin/env python
import collections
import os
import sys
from collections import namedtuple

import yaml

from mqtt2msg import const
from mqtt2msg import log

CFG_FILENAME = os.path.dirname(os.path.abspath(const.__file__)) + "/../data/config.yaml"
Info = namedtuple("Info", "mqtt knobs cfg_globals topics raw_cfg")


class Cfg(object):
    _info = None  # class (or static) variable

    def __init__(self):
        pass

    @property
    def mqtt_host(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get("host")
        return None

    @property
    def mqtt_client_id(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get("client_id", const.MQTT_CLIENT_ID_DEFAULT)
        return const.MQTT_CLIENT_ID_DEFAULT

    @property
    def mqtt_message_topic(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get("message_topic", const.MQTT_CLIENT_DEFAULT_TOPIC_MESSAGE)
        return const.MQTT_CLIENT_DEFAULT_TOPIC_MESSAGE

    @property
    def knobs(self):
        return self._get_info().knobs

    @property
    def topics(self):
        return self._get_info().topics

    @classmethod
    def _get_config_filename(cls):
        if len(sys.argv) > 1:
            return sys.argv[1]
        return CFG_FILENAME

    @classmethod
    def _get_info(cls):
        if not cls._info:
            config_filename = cls._get_config_filename()
            logger.info("loading yaml config file %s", config_filename)
            with open(config_filename, "r") as ymlfile:
                raw_cfg = yaml.safe_load(ymlfile)
                cls._parse_raw_cfg(raw_cfg)
        return cls._info

    @classmethod
    def _parse_raw_cfg(cls, raw_cfg):
        cfg_globals = raw_cfg.get("globals", {})
        assert isinstance(cfg_globals, dict)
        topics = raw_cfg.get("topics")
        assert isinstance(topics, dict)

        cls._info = Info(
            raw_cfg.get("mqtt"), raw_cfg.get("knobs", {}), cfg_globals, topics, raw_cfg
        )


# =============================================================================


logger = log.getLogger()
if __name__ == "__main__":
    log.initLogger()
    c = Cfg()
    logger.info("c.knobs: {}".format(c.knobs))
    logger.info("c.mqtt_host: {}".format(c.mqtt_host))
    logger.info("c.cfg_globals: {}".format(c.cfg_globals))
    logger.info("c.topics: {}".format(c.topics))
