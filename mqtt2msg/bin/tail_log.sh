#!/bin/bash

if [ -z "$1" ]; then
    sudo journalctl -u mqtt2msg.service --no-pager --follow
else
    sudo tail -F /var/log/syslog | grep mqtt2msg
fi
