#!/usr/bin/env bash
# The Mac's IPv4 addresses now, space-separated (loopback and link-local aside): make up and make run hand them to the
# robot agent as MAC_AGENT_HOSTS, which it tries in this order (robot_agent/config.py). The address of the interface
# the Mac's own traffic leaves by comes first: docked, that is the wired one, and its Wi-Fi dozes meanwhile.
primary=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
first=$([ -n "$primary" ] && ipconfig getifaddr "$primary" 2>/dev/null)
{
    echo "$first"
    ifconfig | awk '/inet / {print $2}'
} | awk 'NF && $1 != "127.0.0.1" && $1 !~ /^169\.254\./ && !seen[$1]++' | paste -sd ' ' -
