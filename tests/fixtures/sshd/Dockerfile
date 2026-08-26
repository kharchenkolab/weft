# Mock "remote workstation": glibc userland (conda envs need it), sshd,
# rsync for the data plane, curl for the internet probe.
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    openssh-server rsync curl ca-certificates time \
    squashfs-tools squashfuse fuse3 uidmap iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /run/sshd
RUN useradd -m -s /bin/bash physicist
# key injection at RUN time (docker-entrypoint.sh) — a baked key
# made the shared tag mutable state across fixtures (R1b)
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
EXPOSE 22
CMD ["/docker-entrypoint.sh"]
