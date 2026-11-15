"""Run the k6 load profile against a running Compose stack and record resource usage.

    python scripts/run_load_test.py --profile smoke|moderate|stress|sustained [--project nexus-p4]

Starts grafana/k6 (pinned) on the Compose network (API at http://api:8000, Keycloak at
http://keycloak:8080), samples `docker stats` for every container of the project while k6 runs,
and writes eval/phase4/load/<profile>.json: the k6 summary as measured (never edited), CPU / memory
per container (max / mean of the samples), and the host and Docker environment.
"""
import argparse
import json
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K6_IMAGE = "grafana/k6:1.2.3"


def docker_stats(project: str, samples: list, stop: threading.Event) -> None:
    while not stop.is_set():
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], capture_output=True,
                             text=True, timeout=60).stdout
        stamp = time.time()
        for line in out.splitlines():
            row = json.loads(line)
            if row["Name"].startswith(project + "-"):
                samples.append({"t": stamp, "name": row["Name"], "cpu": row["CPUPerc"], "mem": row["MemUsage"]})
        stop.wait(5)


def _num(text: str) -> float:
    text = text.strip()
    units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "kB": 1 / 1000, "MB": 1, "GB": 1000, "B": 1 / (1024 * 1024)}
    for unit, factor in units.items():
        if text.endswith(unit):
            return float(text[: -len(unit)]) * factor
    return float(text.rstrip("%"))


def summarize(samples: list) -> dict:
    out: dict[str, dict] = {}
    for s in samples:
        service = s["name"].split("-")[-2] if s["name"].count("-") >= 2 else s["name"]
        entry = out.setdefault(service, {"cpu": [], "mem_mib": []})
        entry["cpu"].append(_num(s["cpu"]))
        entry["mem_mib"].append(_num(s["mem"].split("/")[0]))
    return {k: {"samples": len(v["cpu"]), "cpu_percent_max": round(max(v["cpu"]), 1),
                "cpu_percent_mean": round(sum(v["cpu"]) / len(v["cpu"]), 1),
                "mem_mib_max": round(max(v["mem_mib"]), 1)} for k, v in out.items() if v["cpu"]}


def environment() -> dict:
    info = json.loads(subprocess.run(["docker", "info", "--format", "{{json .}}"], capture_output=True,
                                     text=True).stdout or "{}")
    cpu = platform.processor()
    try:
        if sys.platform == "win32":
            cpu = subprocess.run(["wmic", "cpu", "get", "name"], capture_output=True, text=True).stdout.split("\n")[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return {"host_os": platform.platform(), "host_cpu": cpu, "docker_server": info.get("ServerVersion"),
            "docker_cpus": info.get("NCPU"), "docker_mem_gib": round((info.get("MemTotal") or 0) / 2 ** 30, 1),
            "docker_os": info.get("OperatingSystem"), "k6_image": K6_IMAGE}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="smoke", choices=["smoke", "moderate", "stress", "sustained"])
    parser.add_argument("--project", default="nexus-p4")
    parser.add_argument("--network", default="")
    args = parser.parse_args()
    out_dir = ROOT / "eval" / "phase4" / "load"
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = out_dir / f".k6-{args.profile}"
    scratch.mkdir(exist_ok=True)
    network = args.network or f"{args.project}_default"
    samples: list = []
    stop = threading.Event()
    sampler = threading.Thread(target=docker_stats, args=(args.project, samples, stop), daemon=True)
    sampler.start()
    started = time.time()
    cmd = ["docker", "run", "--rm", "--network", network, "-e", f"PROFILE={args.profile}",
           "-v", f"{ROOT / 'load' / 'k6'}:/scripts:ro", "-v", f"{scratch}:/out", K6_IMAGE, "run", "--quiet",
           "/scripts/nexus.js"]
    run = subprocess.run(cmd, capture_output=True, text=True)
    stop.set()
    sampler.join(timeout=10)
    summary_file = scratch / "summary.json"
    k6 = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.exists() else None
    report = {"profile": args.profile, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
              "wall_seconds": round(time.time() - started, 1), "k6_exit_code": run.returncode,
              "environment": environment(), "resources": summarize(samples), "k6": k6,
              "k6_stderr_tail": run.stderr[-2000:]}
    (out_dir / f"{args.profile}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("profile", "wall_seconds", "k6_exit_code", "resources")}, indent=2))
    if k6:
        print("authz_violations:", k6.get("authz_violations"), "| thresholds_ok:", k6.get("thresholds_ok"))
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
