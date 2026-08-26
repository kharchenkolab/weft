#!/bin/sh
set -e
if [ ! -f /etc/munge/munge.key ]; then
    dd if=/dev/urandom of=/etc/munge/munge.key bs=1024 count=1 2>/dev/null
    chown munge:munge /etc/munge/munge.key
    chmod 400 /etc/munge/munge.key
fi
mkdir -p /run/munge && chown munge:munge /run/munge
runuser -u munge -- /usr/sbin/munged
if [ -f /run/host-key.pub ]; then
    mkdir -p /home/physicist/.ssh
    cp /run/host-key.pub /home/physicist/.ssh/authorized_keys
    chown -R physicist:physicist /home/physicist/.ssh
    chmod 700 /home/physicist/.ssh
    chmod 600 /home/physicist/.ssh/authorized_keys
fi
/usr/sbin/sshd
/usr/sbin/slurmctld
/usr/sbin/slurmd
exec tail -f /dev/null
