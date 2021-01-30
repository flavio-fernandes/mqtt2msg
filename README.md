# mqtt2msg
#### Python-based project that monitors MQTT topics to publish configured messages

Based on MQTT subscriptions, aggregate distinct topics with well-known
payloads into simple messages. A usage case would be to simplify the monitoring
of multiple motion sensor topics by only needing to check for messages at
a common MQTT topic.

The topics to be monitored, as well as their
corresponding messages are configured through a YAML file.

```yaml
mqtt:
    host: 192.168.123.123
    message_topic: /msg
topics:
    /laundry/motion:
        msg: 'movement in laundry room'
        priority: 1
        expiration: 20
    /garage/motion:
        msg: 'movement in garage'
        'on': [ 'on', 'enabled' ]
        'off': 'disabled'
```

Here is an example where mqtt2msg is handling the yaml file shown above
```shell script
$ ./mqtt2msg/bin/create-env.sh && \
  source ./env/bin/activate && \
  export PYTHONPATH=${PWD}

$ python3 mqtt2msg/main.py ./data/config.yaml
```

Monitoring /msg topic sample output (observed when we do the publish commands below)
```shell script
$ MQTT=192.168.123.123 && \
  mosquitto_sub -F '@Y-@m-@dT@H:@M:@S@z : %q : %t : %p' -h $MQTT -t /msg

2021-01-30T21:43:03-0500 : 0 : /msg : movement in laundry room
2021-01-30T21:43:27-0500 : 0 : /msg :
2021-01-30T21:43:29-0500 : 0 : /msg : movement in garage
2021-01-30T21:43:33-0500 : 0 : /msg :
```

Publishing events that mqtt2msg is aggregating
```shell script
$ MQTT=192.168.123.123 && \
  mosquitto_pub -h $MQTT -t /laundry/motion -m on && \
  mosquitto_pub -h $MQTT -t /garage/motion -m enabled && \
  sleep 30 && \
  mosquitto_pub -h $MQTT -t /garage/motion -m disabled && \
  echo ok
```


To have a notion of importance, you can use the
[priority](https://github.com/flavio-fernandes/mqtt2msg/blob/df1b11268c58f5566e99b4c209b3c30dce481d69/data/config.yaml.vagrant#L21)
attribute. It is also possible to watch out for
[high/low](https://github.com/flavio-fernandes/mqtt2msg/blob/df1b11268c58f5566e99b4c209b3c30dce481d69/data/config.yaml.vagrant#L26-L27)
watermark triggers for things like temperature.

Take a look at [data/config.yaml.vagrant](https://github.com/flavio-fernandes/mqtt2msg/blob/master/data/config.yaml.vagrant)
to see an example with all the attributes that can be used. The
[Vagrantfile](https://github.com/flavio-fernandes/mqtt2msg/blob/master/Vagrantfile) can also be used as
a reference, together with
[the test](https://github.com/flavio-fernandes/mqtt2msg/blob/master/mqtt2msg/tests/basic_test.sh.vagrant)
that exercises that config.

**NOTE:** Use python 3.9 or newer, as this project requires a somewhat
recent implementation of [asyncio](https://realpython.com/async-io-python/).