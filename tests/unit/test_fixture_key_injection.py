"""The shared-image-tag rule: fixture sshd images carry NO baked
authorized_keys — the caller's pubkey mounts at run time. `weft-test-
sshd` is ONE tag built by four fixtures with different session keydirs;
when the key was baked, each rebuild silently re-keyed the tag and any
test starting a NEW container with an earlier fixture's key got
Permission denied — in-lane only, fresh-containers only, masquerading
as 'lane load' through three R1b forensics rounds. Two pins: the build
scripts must not bake, and every docker-run of the shared tags must
mount /run/host-key.pub."""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures"
SELF = Path(__file__).name


def test_build_scripts_do_not_bake_keys():
    for script in ("sshd/build.sh", "slurm/build.sh"):
        text = (FIXTURES / script).read_text()
        assert "authorized_keys" not in text, (
            f"{script} bakes a key into the shared tag again")


def test_every_shared_tag_launch_mounts_the_key():
    tag = re.compile(r'"weft-test-(?:sshd|slurm)"')
    offences = []
    for py in sorted(TESTS.rglob("*.py")):
        if py.name == SELF:
            continue
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if not tag.search(line):
                continue
            window = lines[max(0, i - 8):i + 2]
            if not any("host-key.pub" in w for w in window):
                offences.append(f"{py.relative_to(TESTS)}:{i + 1}")
    assert not offences, (
        "docker-run of a shared sshd tag without the key mount "
        f"(-v <pub>:/run/host-key.pub:ro): {offences}")
