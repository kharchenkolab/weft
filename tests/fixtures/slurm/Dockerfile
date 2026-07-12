# Single-container Slurm "cluster": slurmctld + slurmd + munge + sshd.
# Login node semantics for weft (ssh in, sbatch/squeue/scontrol available);
# the same container hosts the compute daemon — storage is trivially shared,
# which matches the common university-cluster layout weft assumes by default.
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    slurm-wlm munge openssh-server rsync curl ca-certificates time \
    environment-modules \
    && rm -rf /var/lib/apt/lists/* && mkdir /run/sshd
RUN useradd -m -s /bin/bash physicist
COPY authorized_keys /home/physicist/.ssh/authorized_keys
RUN chown -R physicist:physicist /home/physicist/.ssh \
    && chmod 700 /home/physicist/.ssh && chmod 600 /home/physicist/.ssh/authorized_keys

COPY slurm.conf /etc/slurm/slurm.conf
COPY gres.conf /etc/slurm/gres.conf
RUN mkdir -p /var/spool/slurmctld /var/spool/slurmd /var/log/slurm \
    && chown slurm:slurm /var/spool/slurmctld /var/log/slurm \
    && chmod 755 /var/spool/slurmctld

# fake accounting/fairshare (no slurmdbd in the fixture): /usr/local/bin
# shadows the real binaries, which would only error without a DB
COPY fake-sacctmgr /usr/local/bin/sacctmgr
COPY fake-sshare /usr/local/bin/sshare
RUN chmod +x /usr/local/bin/sacctmgr /usr/local/bin/sshare

# a site-licensed "module" the tests can require (doc scenario S4)
COPY modulefiles /opt/site-modules
ENV MODULEPATH=/opt/site-modules

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 22
CMD ["/entrypoint.sh"]
