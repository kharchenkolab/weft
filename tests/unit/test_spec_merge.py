import pytest

from weft.errors import WeftError
from weft.spec import EnvSpec, resolve_extends, split_constraint


def test_split_constraint():
    assert split_constraint("root >=6.32") == ("root", ">=6.32")
    assert split_constraint("numpy") == ("numpy", "*")
    assert split_constraint("python =3.12") == ("python", "=3.12")
    assert split_constraint("zfit ==0.24.*") == ("zfit", "==0.24.*")
    assert split_constraint("Cython") == ("cython", "*")


def _base():
    return EnvSpec.from_dict(
        {
            "name": "base-physics",
            "platforms": ["linux-64", "osx-arm64"],
            "channels": ["conda-forge"],
            "deps": {
                "conda": ["python =3.12", "numpy >=1.26", "scipy", "matplotlib"],
                "pypi": [],
            },
            "env_vars": {"OMP_NUM_THREADS": "1"},
        }
    )


def test_child_constraint_overrides_in_place():
    child = EnvSpec.from_dict({"deps": {"conda": ["numpy ==2.0.1", "emcee"]}})
    merged = child.merged_onto(_base())
    assert merged.conda == ["python =3.12", "numpy ==2.0.1", "scipy", "matplotlib", "emcee"]


def test_channels_prepend_dedup():
    parent = _base()
    parent.channels = ["conda-forge", "nvidia"]
    child = EnvSpec.from_dict({"channels": ["my-lab-channel", "conda-forge"], "deps": {}})
    merged = child.merged_onto(parent)
    assert merged.channels == ["my-lab-channel", "conda-forge", "nvidia"]


def test_scalar_and_dict_merges():
    parent = _base()
    child = EnvSpec.from_dict(
        {
            "deps": {},
            "env_vars": {"OMP_NUM_THREADS": "{{cpus}}", "EXTRA": "1"},
            "container_base": "docker.io/library/ubuntu:24.04",
            "modules": ["espresso/7.2"],
            "post_install": ["pip install ./vendored-tool"],
        }
    )
    merged = child.merged_onto(parent)
    assert merged.env_vars == {"OMP_NUM_THREADS": "{{cpus}}", "EXTRA": "1"}
    assert merged.container_base == "docker.io/library/ubuntu:24.04"
    assert merged.modules == ["espresso/7.2"]
    assert merged.post_install == ["pip install ./vendored-tool"]
    assert merged.weakly_reproducible()
    assert not parent.weakly_reproducible()


def test_variant_merge():
    parent = _base()
    parent.variants = {"linux-64": {"conda": ["cuda-version =12.4"], "pypi": []}}
    child = EnvSpec.from_dict(
        {"deps": {}, "variants": {"linux-64": {"conda": ["cuda-version =12.6", "cupy"]}}}
    )
    merged = child.merged_onto(parent)
    assert merged.variants["linux-64"]["conda"] == ["cuda-version =12.6", "cupy"]


def test_extends_chain_and_cycle():
    store: dict[str, EnvSpec] = {}
    base = _base()
    store[base.spec_hash()] = base
    mid = EnvSpec.from_dict({"extends": base.spec_hash(), "deps": {"conda": ["iminuit"]}})
    store[mid.spec_hash()] = mid
    leaf = EnvSpec.from_dict({"extends": mid.spec_hash(), "deps": {"conda": ["emcee"]}})

    merged = resolve_extends(leaf, store.get)
    assert "iminuit" in merged.conda and "emcee" in merged.conda
    assert merged.extends is None

    looped = EnvSpec.from_dict({"extends": "", "deps": {}})
    looped.extends = looped.spec_hash()
    store[looped.extends] = looped
    with pytest.raises(WeftError) as e:
        resolve_extends(looped, store.get)
    assert e.value.code == "task.invalid"


def test_unknown_field_rejected_with_hint():
    with pytest.raises(WeftError) as e:
        EnvSpec.from_dict({"dependencies": {"conda": ["numpy"]}})
    assert "known_fields" in e.value.hints


def test_spec_hash_stable_and_order_sensitive():
    a = _base()
    b = _base()
    assert a.spec_hash() == b.spec_hash()
    b.conda = list(reversed(b.conda))
    assert a.spec_hash() != b.spec_hash()  # dep order is meaning (override order)
