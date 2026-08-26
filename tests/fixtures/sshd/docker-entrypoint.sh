#!/bin/sh
# The caller's pubkey arrives at RUN time (-v <pub>:/run/host-key.pub:ro).
# It must NOT be baked into the image: `weft-test-sshd` is ONE shared
# tag built by several fixtures with DIFFERENT session keydirs — each
# rebuild silently re-keyed the tag, and every test that started a NEW
# container with an earlier fixture's key got Permission denied. Live
# containers kept their creation-time key, so the failures looked like
# "lane load" for three docker-lane forensics rounds (R1b).
set -e
if [ -f /run/host-key.pub ]; then
    mkdir -p /home/physicist/.ssh
    cp /run/host-key.pub /home/physicist/.ssh/authorized_keys
    chown -R physicist:physicist /home/physicist/.ssh
    chmod 700 /home/physicist/.ssh
    chmod 600 /home/physicist/.ssh/authorized_keys
fi
exec /usr/sbin/sshd -D -e
