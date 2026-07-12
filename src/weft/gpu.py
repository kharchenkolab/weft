"""GPU capability -> environment advice (agent leverage point).

CUDA userland from conda-forge must not exceed what the site's *driver*
supports. The probe records the driver's maximum CUDA version; this helper
turns that into the spec pieces the agent should add, plus the pixi
system-requirement that lets the stack solve on a GPU-less controller.

Pure function over the capability record — unit-testable, and honest when
there is nothing to suggest.
"""

from __future__ import annotations

from .capability import compute_view, gpu_count


def suggest_gpu_spec(caps: dict, site_name: str = "") -> dict:
    view = compute_view(caps)
    gpus = view.get("gpus", [])
    driver = view.get("cuda_driver", "")
    if view.get("os") == "darwin" and view.get("arch") in ("arm64", "aarch64"):
        # Apple Silicon: Metal/MPS acceleration ships in the default
        # osx-arm64 builds — no version pins, no metapackages
        return {
            "usable": True,
            "gpus": [{"model": "apple-silicon-metal", "count": 1}],
            "accelerator": "metal-mps",
            "deps": [], "system_requirements": {},
            "note": "default osx-arm64 builds include Metal/MPS support "
                    "(e.g. pytorch uses the 'mps' device); nothing to pin",
        }
    if not gpus:
        return {
            "usable": False,
            "reason": f"no GPUs visible on {site_name or 'site'} "
                      "(nvidia-smi absent or empty)",
            "deps": [], "system_requirements": {},
        }
    if not driver:
        return {
            "usable": False,
            "reason": "GPUs present but driver CUDA version unknown — "
                      "re-probe or check nvidia-smi on the site",
            "gpus": gpus, "deps": [], "system_requirements": {},
        }
    # pin userland at or below the driver's max supported CUDA
    return {
        "usable": True,
        "gpus": gpus,
        "driver_cuda": driver,
        "deps": [f"cuda-version <={driver}"],
        "system_requirements": {"cuda": driver},
        "note": (
            f"add deps to the spec's linux-64 variant along with the GPU "
            f"packages you need; cuda-version <={driver} keeps the userland "
            f"within what the driver supports. Packages with separate CPU/GPU "
            f"builds need the GPU variant forced: use the -gpu metapackage "
            f"(e.g. 'pytorch-gpu') or a build selector ('pytorch 2.* *cuda*')"
        ),
    }
