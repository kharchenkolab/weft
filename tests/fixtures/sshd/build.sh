#!/bin/sh
# Build the sshd fixture image with a session keypair.
# Usage: build.sh <keydir>  — writes id_ed25519{,.pub} into keydir if absent.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
keydir="${1:?usage: build.sh <keydir>}"
mkdir -p "$keydir"
if [ ! -f "$keydir/id_ed25519" ]; then
    ssh-keygen -t ed25519 -N "" -f "$keydir/id_ed25519" -q
fi
# NO key bake: the tag is shared by every fixture; keys are
# injected per-container at run time (-v <pub>:/run/host-key.pub:ro)
docker build -q -t weft-test-sshd "$here"
