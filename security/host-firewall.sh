#!/bin/sh
# Run as root on your Docker host. Touch only project chains; never flush host/Docker rules.
# Matching bridge interfaces also covers replacement containers after restart.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
iptables -w -N BILLBOARD-OUT 2>/dev/null || iptables -w -S BILLBOARD-OUT >/dev/null
iptables -w -N BILLBOARD-HOST 2>/dev/null || iptables -w -S BILLBOARD-HOST >/dev/null
# Replace our chains atomically, with unrelated tables/rules preserved.
{
    echo '*filter'
    echo '-F BILLBOARD-OUT'
    echo '-F BILLBOARD-HOST'
    for prefix in br-bboard; do
        for suffix in in out; do
            bridge="$prefix-$suffix"
            echo "-A BILLBOARD-HOST -i $bridge -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
            echo "-A BILLBOARD-HOST -i $bridge -j DROP"
            echo "-A BILLBOARD-OUT -i $bridge -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
        done
        # The HTTPS frontend initiates no connections outside its isolated backend.
        echo "-A BILLBOARD-OUT -i $prefix-in -j DROP"
        for destination in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
            echo "-A BILLBOARD-OUT -i $prefix-out -d $destination -j DROP"
        done
        echo "-A BILLBOARD-OUT -i $prefix-out -p tcp --dport 443 -j RETURN"
        echo "-A BILLBOARD-OUT -i $prefix-out -p udp --dport 53 -j RETURN"
        echo "-A BILLBOARD-OUT -i $prefix-out -p tcp --dport 53 -j RETURN"
        echo "-A BILLBOARD-OUT -i $prefix-out -j DROP"
    done
    echo COMMIT
} | iptables-restore -w --noflush
iptables -w -C DOCKER-USER -j BILLBOARD-OUT 2>/dev/null || iptables -w -I DOCKER-USER 1 -j BILLBOARD-OUT
iptables -w -C INPUT -j BILLBOARD-HOST 2>/dev/null || iptables -w -I INPUT 1 -j BILLBOARD-HOST
