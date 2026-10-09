"""
Admission control for research runs.

The daemon asks a ResourceManager whether a queued run may start. The local
implementation looks at free RAM, CPU idle, GPU memory (nvidia-smi) and the
number of active runs. To move to Slurm/Kubernetes later, implement the same
three methods (`probe`, `try_acquire`, `release`) and select it in server.yaml;
the research pipeline and CLI do not change.
"""
import shutil
import subprocess
import threading

import psutil


class ResourceSnapshot(dict):
    pass


def probe_gpus():
    """List of {index, memory_total_mb, memory_free_mb}; empty if no NVIDIA GPU."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            gpus.append({"index": int(parts[0]), "memory_total_mb": int(parts[1]), "memory_free_mb": int(parts[2])})
    return gpus


class LocalResourceManager:
    def __init__(self, config, probe=None):
        self.res = config.section("resources")
        self.exe = config.section("execution")
        self._probe = probe or self._default_probe
        self._lock = threading.Lock()
        self.allocations = {}  # run_id -> {"gpus": [...]}

    @staticmethod
    def _default_probe():
        vm = psutil.virtual_memory()
        return ResourceSnapshot(
            cpu_count=psutil.cpu_count() or 1,
            cpu_idle_percent=100.0 - psutil.cpu_percent(interval=0.2),
            ram_total_gb=vm.total / 2**30,
            ram_available_gb=vm.available / 2**30,
            gpus=probe_gpus(),
        )

    def probe(self):
        return self._probe()

    def try_acquire(self, run_id, active_run_count):
        """Return (granted, allocation_or_reason)."""
        with self._lock:
            if run_id in self.allocations:
                return True, self.allocations[run_id]
            snap = self.probe()
            if active_run_count >= int(self.exe.get("max_concurrent_runs", 4)):
                return False, f"server run limit reached ({active_run_count}/{self.exe['max_concurrent_runs']})"
            if snap["ram_available_gb"] < float(self.res.get("min_free_ram_gb", 0)):
                return False, f"insufficient RAM ({snap['ram_available_gb']:.1f} GB free, need {self.res['min_free_ram_gb']})"
            if snap["cpu_idle_percent"] < float(self.res.get("min_free_cpu_percent", 0)):
                return False, f"CPU busy ({snap['cpu_idle_percent']:.0f}% idle)"
            need = int(self.res.get("gpus_per_run", 0))
            gpus = []
            if need > 0:
                taken = {g for a in self.allocations.values() for g in a.get("gpus", [])}
                min_free = int(self.res.get("min_free_gpu_memory_mb", 0))
                free = [g["index"] for g in snap.get("gpus", []) if g["index"] not in taken and g["memory_free_mb"] >= min_free]
                if len(free) < need:
                    return False, f"insufficient GPUs ({len(free)} free, need {need})"
                gpus = free[:need]
            allocation = {"gpus": gpus, "snapshot": {k: v for k, v in snap.items() if k != "gpus"}}
            self.allocations[run_id] = allocation
            return True, allocation

    def release(self, run_id):
        with self._lock:
            return self.allocations.pop(run_id, None) is not None

    def restore(self, run_id, allocation):
        """Re-register an allocation for a run that survived a daemon restart."""
        with self._lock:
            self.allocations[run_id] = allocation or {"gpus": []}
