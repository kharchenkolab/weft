# Mock "remote workstation": glibc userland (conda envs need it), sshd,
# rsync for the data plane, curl for the internet probe.
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    openssh-server rsync curl ca-certificates time \
    squashfs-tools squashfuse fuse3 uidmap \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /run/sshd
RUN useradd -m -s /bin/bash physicist
COPY authorized_keys /home/physicist/.ssh/authorized_keys
RUN chown -R physicist:physicist /home/physicist/.ssh \
    && chmod 700 /home/physicist/.ssh \
    && chmod 600 /home/physicist/.ssh/authorized_keys
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
