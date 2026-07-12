#!/bin/sh
# Hostile-start fixtures: machines you get handed in the real world.
#   weft-test-rocky8 — RHEL8-era HPC node: old glibc 2.28, no python needed
#   weft-test-bare   — debian-slim: no curl, no wget, no rsync, no python
#   weft-test-musl   — alpine: musl libc, busybox userland
# Usage: build.sh <keydir>
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
keydir="${1:?usage: build.sh <keydir>}"
mkdir -p "$keydir"
[ -f "$keydir/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "$keydir/id_ed25519" -q
PUB=$(cat "$keydir/id_ed25519.pub")

build() {
    tag=$1
    docker build -q -t "$tag" -f - "$here" <<EOF
$2
RUN mkdir -p /home/physicist/.ssh && echo '$PUB' > /home/physicist/.ssh/authorized_keys \\
 && chown -R physicist /home/physicist/.ssh && chmod 700 /home/physicist/.ssh \\
 && chmod 600 /home/physicist/.ssh/authorized_keys
EXPOSE 22
# rocky8 images ship /run/nologin (pam: "system is booting up") — drop it
CMD ["/bin/sh", "-c", "rm -f /run/nologin /var/run/nologin 2>/dev/null; exec /usr/sbin/sshd -D -e"]
EOF
}

build weft-test-rocky8 '
FROM rockylinux:8
RUN dnf install -y openssh-server && ssh-keygen -A && mkdir -p /run/sshd \
 && useradd -m physicist && dnf clean all
'

build weft-test-bare '
FROM debian:12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
 && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd \
 && useradd -m -s /bin/bash physicist
'

build weft-test-musl '
FROM alpine:3.20
RUN apk add --no-cache openssh && ssh-keygen -A \
 && adduser -D physicist && passwd -u physicist
'
