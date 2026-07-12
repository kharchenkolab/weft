#!/bin/sh
set -e
if [ ! -f /etc/munge/munge.key ]; then
    dd if=/dev/urandom of=/etc/munge/munge.key bs=1024 count=1 2>/dev/null
    chown munge:munge /etc/munge/munge.key
    chmod 400 /etc/munge/munge.key
fi
mkdir -p /run/munge && chown munge:munge /run/munge
runuser -u munge -- /usr/sbin/munged
/usr/sbin/sshd
/usr/sbin/slurmctld
/usr/sbin/slurmd
exec tail -f /dev/null
