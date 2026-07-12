import textwrap

from weft.lock import canonicalize_lock, render_pixi_manifest
from weft.ids import env_id
from weft.spec import EnvSpec

SYNTHETIC_LOCK = textwrap.dedent(
    """\
    version: 6
    environments:
      default:
        channels:
        - url: https://conda.anaconda.org/conda-forge/
        packages:
          linux-64:
          - conda: https://conda.anaconda.org/conda-forge/linux-64/numpy-2.0.1-py312h1103770_0.conda
          - conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.12.4-h194c7f8_0.conda
          - pypi: https://files.pythonhosted.org/packages/zfit-0.24.3-py3-none-any.whl
          osx-arm64:
          - conda: https://conda.anaconda.org/conda-forge/osx-arm64/python-3.12.4-h30c5eda_0.conda
    packages:
    - conda: https://conda.anaconda.org/conda-forge/linux-64/numpy-2.0.1-py312h1103770_0.conda
      sha256: aaa111
    - conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.12.4-h194c7f8_0.conda
      sha256: bbb222
    - conda: https://conda.anaconda.org/conda-forge/osx-arm64/python-3.12.4-h30c5eda_0.conda
      sha256: ccc333
    - pypi: https://files.pythonhosted.org/packages/zfit-0.24.3-py3-none-any.whl
      name: zfit
      version: 0.24.3
      sha256: ddd444
    """
)


def _spec():
    return EnvSpec.from_dict(
        {"name": "t", "deps": {"conda": ["python =3.12", "numpy"], "pypi": ["zfit"]}}
    )


def test_canonicalization_extracts_sorted_records():
    canon = canonicalize_lock(SYNTHETIC_LOCK, _spec())
    linux = canon["platforms"]["linux-64"]
    assert [r["name"] for r in linux] == ["numpy", "python", "zfit"]
    assert linux[0]["sha256"] == "aaa111"
    assert linux[2] == {
        "kind": "pypi", "name": "zfit", "version": "0.24.3", "build": "", "sha256": "ddd444",
    }
    assert "osx-arm64" in canon["platforms"]


def test_env_id_versioned_and_sensitive_to_extras():
    spec = _spec()
    canon = canonicalize_lock(SYNTHETIC_LOCK, spec)
    eid = env_id(canon)
    assert eid.startswith("env:v1:") and len(eid) == len("env:v1:") + 64

    spec2 = _spec()
    spec2.modules = ["espresso/7.2"]
    canon2 = canonicalize_lock(SYNTHETIC_LOCK, spec2)
    assert env_id(canon2) != eid  # modules alter realization, so alter identity

    # identical resolution from a differently-ordered spec shares the EnvID
    spec3 = EnvSpec.from_dict(
        {"name": "other", "deps": {"conda": ["numpy", "python =3.12"], "pypi": ["zfit"]}}
    )
    canon3 = canonicalize_lock(SYNTHETIC_LOCK, spec3)
    assert env_id(canon3) == eid


def test_manifest_rendering():
    spec = EnvSpec.from_dict(
        {
            "name": "m",
            "platforms": ["linux-64"],
            "deps": {"conda": ["python =3.12", "root >=6.32"], "pypi": ["zfit ==0.24.*"]},
            "variants": {"linux-64": {"conda": ["cuda-version =12.4"]}},
        }
    )
    text = render_pixi_manifest(spec)
    assert '"python" = "3.12.*"' in text          # conda fuzzy '=' normalized
    assert '"root" = ">=6.32"' in text
    assert '"zfit" = "==0.24.*"' in text
    assert "[target.linux-64.dependencies]" in text
    assert '"cuda-version" = "12.4.*"' in text
