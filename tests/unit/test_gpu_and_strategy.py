import pytest

from weft.errors import WeftError
from weft.gpu import suggest_gpu_spec
from weft.placement import rank_sites
from weft.strategy import select_strategy


def _gpu_caps(cuda="12.4", gpus=None):
    return {
        "os": "linux", "arch": "x86_64", "cpus": 64, "mem_gb": 256,
        "internet": True, "runtimes": {"apptainer": "1.3.1", "docker": False},
        "scheduler": {"type": "slurm"}, "module_system": True,
        "gpus": gpus if gpus is not None else [{"model": "A100-40GB", "count": 4}],
        "cuda_driver": cuda, "storage": {},
    }


def test_gpu_hint_pins_to_driver():
    s = suggest_gpu_spec(_gpu_caps("12.4"), "hpc")
    assert s["usable"] and s["deps"] == ["cuda-version <=12.4"]
    assert s["system_requirements"] == {"cuda": "12.4"}


def test_gpu_hint_apple_silicon_metal():
    caps = {"os": "darwin", "arch": "arm64", "cpus": 10, "mem_gb": 32,
            "internet": True, "runtimes": {}, "scheduler": {"type": "none"},
            "gpus": [], "cuda_driver": "", "storage": {}}
    s = suggest_gpu_spec(caps, "laptop")
    assert s["usable"] and s["accelerator"] == "metal-mps"
    assert s["deps"] == [] and s["system_requirements"] == {}


def test_gpu_hint_honest_when_no_gpu_or_no_driver_info():
    s = suggest_gpu_spec(_gpu_caps(gpus=[]), "laptop")
    assert not s["usable"] and "no GPUs" in s["reason"]
    s2 = suggest_gpu_spec(_gpu_caps(cuda=""), "hpc")
    assert not s2["usable"] and "re-probe" in s2["reason"]


def test_placement_rejects_gpu_ask_on_cpu_site():
    sites = [
        {"name": "laptop", "health": "ok",
         "capabilities": _gpu_caps(gpus=[])},
        {"name": "hpc-gpu", "health": "ok", "capabilities": _gpu_caps()},
    ]
    r = rank_sites({"cpus": 4, "gpus": 1}, [], sites, set(), {}, 0)
    assert [x["site"] for x in r["ranked"]] == ["hpc-gpu"]
    rej = next(x for x in r["rejected"] if x["site"] == "laptop")
    assert rej["hints"]["gpus"]["max"] == 0  # nearest valid ask, machine-readable


# -- strategy decision table: exhaustive over the capability axes ------------

CASES = [
    # (internet, apptainer, modules, container_base, expected)
    (True,  False, [],            None,  "prefix"),
    (True,  True,  [],            None,  "prefix"),
    (False, True,  [],            None,  "container"),
    (False, False, [],            None,  "packed"),
    (True,  False, ["espresso"],  None,  "modules+prefix"),
    (False, False, ["espresso"],  None,  "modules+packed"),
    (False, True,  ["espresso"],  None,  "modules+packed"),  # modules never in container
    (True,  True,  [],            "docker.io/u:1", "container"),
]


@pytest.mark.parametrize("internet,apptainer,modules,base,expected", CASES)
def test_strategy_table(internet, apptainer, modules, base, expected):
    caps = {
        "internet": internet,
        "runtimes": {"apptainer": "1.3" if apptainer else "", "docker": False},
        "gpus": [],
    }
    assert select_strategy(caps, modules=modules, container_base=base) == expected


def test_strategy_conflicts_are_structured():
    caps = {"internet": True, "runtimes": {"apptainer": "", "docker": False}, "gpus": []}
    with pytest.raises(WeftError) as e:
        select_strategy(caps, container_base="docker.io/u:1")
    assert e.value.code == "env.unsatisfiable_on_site"
    with pytest.raises(WeftError) as e2:
        select_strategy(caps, modules=["m"], container_base="docker.io/u:1")
    assert e2.value.code == "task.invalid"
    # explicit override validated, not blindly obeyed
    caps_no_net = {"internet": False, "runtimes": {"apptainer": "", "docker": False}, "gpus": []}
    with pytest.raises(WeftError) as e3:
        select_strategy(caps_no_net, prefer="prefix")
    assert e3.value.code == "env.unsatisfiable_on_site"
    assert "packed" in e3.value.hints["alternatives"]


def test_manifest_renders_system_requirements():
    from weft.lock import render_pixi_manifest
    from weft.spec import EnvSpec
    spec = EnvSpec.from_dict({
        "name": "gpu-fit",
        "deps": {"conda": ["python =3.12"]},
        "variants": {"linux-64": {"conda": ["cuda-version <=12.4", "cupy"]}},
        "system_requirements": {"cuda": "12.4"},
    })
    text = render_pixi_manifest(spec)
    assert "[system-requirements]" in text and 'cuda = "12.4"' in text
    # merge: child overrides per key
    child = EnvSpec.from_dict({"deps": {}, "system_requirements": {"cuda": "12.6"}})
    merged = child.merged_onto(spec)
    assert merged.system_requirements == {"cuda": "12.6"}
