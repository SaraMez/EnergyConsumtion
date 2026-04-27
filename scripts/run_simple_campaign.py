#!/usr/bin/env python3
"""Simplified JMH campaign runner with external repetitions and Kwollect summaries."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def log(message: str = "") -> None:
    print(message, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"Unsupported timestamp type: {type(value)!r}")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        match = re.fullmatch(
            r"(?P<prefix>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
            r"(?:\.(?P<fraction>\d+))?"
            r"(?P<tz>Z|[+-]\d{2}:\d{2})?",
            candidate,
        )
        if not match:
            raise

        fraction = (match.group("fraction") or "0")[:6].ljust(6, "0")
        timezone_part = match.group("tz") or "+00:00"
        normalized = f"{match.group('prefix')}.{fraction}{timezone_part}"
        return datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


def short_hostname() -> str:
    return socket.gethostname().split(".")[0]


def slugify(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


def mean_or_none(values: list[float | None]) -> float | None:
    filtered = [value for value in values if value is not None]
    return (sum(filtered) / len(filtered)) if filtered else None


def sample_stdev_or_none(values: list[float | None]) -> float | None:
    filtered = [value for value in values if value is not None]
    if len(filtered) < 2:
        return None
    mean_value = sum(filtered) / len(filtered)
    variance = sum((value - mean_value) ** 2 for value in filtered) / (len(filtered) - 1)
    return math.sqrt(variance)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["timestamp", "device_id", "metric_id", "value", "labels"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp": row.get("timestamp"),
                    "device_id": row.get("device_id"),
                    "metric_id": row.get("metric_id"),
                    "value": row.get("value"),
                    "labels": json.dumps(row.get("labels", {}), sort_keys=True),
                }
            )


def write_campaign_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "version",
        "successful_runs",
        "mean_duration_seconds",
        "mean_jmh_score",
        "score_unit",
        "mean_average_power_w",
        "stdev_average_power_w",
        "mean_idle_average_power_w",
        "stdev_idle_average_power_w",
        "mean_energy_j",
        "stdev_energy_j",
        "mean_net_energy_j",
        "stdev_net_energy_j",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})


def compute_power_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "samples": 0,
            "duration_seconds": 0.0,
            "average_power_w": None,
            "peak_power_w": None,
            "energy_j": None,
        }

    ordered = sorted(records, key=lambda item: parse_timestamp(item["timestamp"]))
    samples = [
        (parse_timestamp(item["timestamp"]), float(item["value"]))
        for item in ordered
        if item.get("value") is not None
    ]
    if not samples:
        return {
            "samples": 0,
            "duration_seconds": 0.0,
            "average_power_w": None,
            "peak_power_w": None,
            "energy_j": None,
        }

    peak_power = max(value for _, value in samples)
    if len(samples) == 1:
        return {
            "samples": 1,
            "duration_seconds": 0.0,
            "average_power_w": samples[0][1],
            "peak_power_w": peak_power,
            "energy_j": 0.0,
        }

    area_j = 0.0
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        area_j += ((p0 + p1) / 2.0) * max(0.0, t1 - t0)

    duration = max(0.0, samples[-1][0] - samples[0][0])
    average_power = area_j / duration if duration > 0 else samples[0][1]
    return {
        "samples": len(samples),
        "duration_seconds": duration,
        "average_power_w": average_power,
        "peak_power_w": peak_power,
        "energy_j": area_j,
    }


def build_kwollect_url(
    *,
    site: str,
    node: str,
    start_epoch: int,
    end_epoch: int,
    metrics: list[str],
    job_id: str | None,
) -> str:
    query = {
        "nodes": node,
        "start_time": str(start_epoch),
        "end_time": str(end_epoch),
        "metrics": ",".join(metrics),
    }
    if job_id:
        query["job_id"] = job_id
    return (
        f"https://api.grid5000.fr/stable/sites/{site}/metrics?"
        f"{urllib.parse.urlencode(query)}"
    )


def fetch_kwollect_metrics(
    *,
    site: str,
    node: str,
    start_epoch: int,
    end_epoch: int,
    metrics: list[str],
    job_id: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    url = build_kwollect_url(
        site=site,
        node=node,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        metrics=metrics,
        job_id=job_id,
    )
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return url, json.load(response)


def locate_jmh_jar() -> Path:
    candidates = sorted((REPO_ROOT / "build" / "libs").glob("*-jmh.jar"))
    if not candidates:
        raise FileNotFoundError("No JMH jar found in build/libs after jmhJar build.")
    return candidates[-1]


def build_gradle_command(args: argparse.Namespace, version: str) -> list[str]:
    return [
        args.gradle_command,
        "--no-daemon",
        "--rerun-tasks",
        "jmhJar",
        f"-PecVersion={version}",
    ]


def build_jmh_command(
    args: argparse.Namespace,
    run_dir: Path,
    jar_path: Path,
) -> tuple[list[str], Path]:
    result_extension = args.jmh_result_format.lower()
    result_path = run_dir / f"jmh-results.{result_extension}"
    command = [
        args.java_command,
        "-jar",
        str(jar_path),
        args.includes or ".*",
        "-rf",
        args.jmh_result_format,
        "-rff",
        str(result_path),
        "-o",
        str(run_dir / "jmh-human.txt"),
        "-i",
        str(args.iterations),
        "-wi",
        str(args.warmup_iterations),
        "-f",
        str(args.forks),
        "-r",
        args.iteration_time,
        "-w",
        args.warmup_time,
        "-tu",
        args.time_unit,
        "-bm",
        args.benchmark_mode,
    ]
    if args.excludes:
        command.extend(["-e", args.excludes])
    for entry in args.benchmark_param:
        command.extend(["-p", entry])
    return command, result_path


def run_process(command: list[str], cwd: Path, log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log_handle.write(line)
        return process.wait()


def load_jmh_summary(result_path: Path) -> dict[str, Any]:
    if not result_path.exists():
        return {"benchmarks": 0, "mean_score": None, "score_unit": None}
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"benchmarks": 0, "mean_score": None, "score_unit": None}
    if not isinstance(payload, list):
        return {"benchmarks": 0, "mean_score": None, "score_unit": None}

    scores: list[float] = []
    score_unit: str | None = None
    for entry in payload:
        metric = entry.get("primaryMetric", {}) if isinstance(entry, dict) else {}
        score = metric.get("score")
        unit = metric.get("scoreUnit")
        if isinstance(score, (int, float)):
            scores.append(float(score))
        if isinstance(unit, str) and score_unit is None:
            score_unit = unit
    return {
        "benchmarks": len(payload),
        "mean_score": mean_or_none(scores),
        "score_unit": score_unit,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run simplified JMH + Kwollect campaigns.")
    parser.add_argument("--versions", nargs="+", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--node", default=short_hostname())
    parser.add_argument("--job-id", default=os.environ.get("OAR_JOB_ID"))
    parser.add_argument("--metrics", nargs="+", default=["wattmetre_power_watt"])
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--gradle-command", default="./gradlew" if os.name != "nt" else "gradlew.bat")
    parser.add_argument("--java-command", default="java")
    parser.add_argument("--includes", default=None)
    parser.add_argument("--excludes", default=None)
    parser.add_argument("--benchmark-param", action="append", default=[])
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmup-iterations", type=int, default=2)
    parser.add_argument("--forks", type=int, default=1)
    parser.add_argument("--iteration-time", default="1s")
    parser.add_argument("--warmup-time", default="1s")
    parser.add_argument("--time-unit", default="ms")
    parser.add_argument("--benchmark-mode", default="avgt")
    parser.add_argument("--run-count", type=int, default=2)
    parser.add_argument("--idle-seconds", type=int, default=30)
    parser.add_argument("--kwollect-settle-seconds", type=int, default=10)
    parser.add_argument("--inter-iteration-seconds", type=int, default=15)
    parser.add_argument("--rest-seconds", type=int, default=10)
    parser.add_argument("--skip-kwollect", action="store_true")
    parser.add_argument(
        "--jmh-result-format",
        default="JSON",
        choices=["JSON", "CSV", "TEXT", "SCSV", "NONE"],
    )
    return parser.parse_args()


def summarize_version(version: str, iteration_rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in iteration_rows if row.get("exit_code") == 0]
    jmh_scores = [row.get("jmh_summary", {}).get("mean_score") for row in successful]
    benchmark_powers = [
        row.get("kwollect_benchmark_summary", {}).get("average_power_w") for row in successful
    ]
    idle_powers = [
        row.get("kwollect_idle_summary", {}).get("average_power_w") for row in successful
    ]
    benchmark_energies = [
        row.get("kwollect_benchmark_summary", {}).get("energy_j") for row in successful
    ]
    net_energies = [row.get("net_energy_j") for row in successful]
    return {
        "version": version,
        "successful_runs": len(successful),
        "mean_duration_seconds": mean_or_none([row.get("duration_seconds") for row in successful]),
        "stdev_duration_seconds": sample_stdev_or_none(
            [row.get("duration_seconds") for row in successful]
        ),
        "mean_jmh_score": mean_or_none(jmh_scores),
        "stdev_jmh_score": sample_stdev_or_none(jmh_scores),
        "score_unit": next(
            (
                row.get("jmh_summary", {}).get("score_unit")
                for row in successful
                if row.get("jmh_summary", {}).get("score_unit")
            ),
            None,
        ),
        "mean_average_power_w": mean_or_none(benchmark_powers),
        "stdev_average_power_w": sample_stdev_or_none(benchmark_powers),
        "mean_idle_average_power_w": mean_or_none(idle_powers),
        "stdev_idle_average_power_w": sample_stdev_or_none(idle_powers),
        "mean_energy_j": mean_or_none(benchmark_energies),
        "stdev_energy_j": sample_stdev_or_none(benchmark_energies),
        "mean_net_energy_j": mean_or_none(net_energies),
        "stdev_net_energy_j": sample_stdev_or_none(net_energies),
    }


def main() -> int:
    args = parse_args()

    started_at = utc_now()
    default_output_dir = REPO_ROOT / "build" / "campaigns" / started_at.strftime("%Y%m%dT%H%M%SZ")
    campaign_dir = Path(args.output_dir) if args.output_dir else default_output_dir
    campaign_dir.mkdir(parents=True, exist_ok=True)

    campaign_rows: list[dict[str, Any]] = []
    for version_index, version in enumerate(args.versions, start=1):
        version_dir = campaign_dir / slugify(version)
        version_dir.mkdir(parents=True, exist_ok=True)

        log("")
        log("=" * 60)
        log(f"  VERSION {version}")
        log("=" * 60)

        gradle_command = build_gradle_command(args, version)
        log(f"  [CMD] {' '.join(gradle_command)}")
        gradle_exit_code = run_process(gradle_command, REPO_ROOT, version_dir / "gradle-build.log")
        if gradle_exit_code != 0:
            return gradle_exit_code

        jar_path = locate_jmh_jar()
        iteration_rows: list[dict[str, Any]] = []

        for run_index in range(1, args.run_count + 1):
            iter_dir = version_dir / f"iter_{run_index:02d}"
            iter_dir.mkdir(parents=True, exist_ok=True)

            log("")
            log(f"  -- Iteration {run_index}/{args.run_count} --")
            row: dict[str, Any] = {"version": version, "iteration_index": run_index}

            if args.idle_seconds > 0:
                log(f"  [idle {args.idle_seconds}s - mesure puissance de base ...]")
                idle_start = utc_now()
                time.sleep(args.idle_seconds)
                idle_end = utc_now()
                row["idle_window"] = {
                    "start": isoformat_utc(idle_start),
                    "end": isoformat_utc(idle_end),
                }

            jmh_command, result_path = build_jmh_command(args, iter_dir, jar_path)
            log(f"  [CMD] {' '.join(jmh_command)}")
            started = utc_now()
            exit_code = run_process(jmh_command, REPO_ROOT, iter_dir / "jmh-run.log")
            ended = utc_now()
            row["started_at"] = isoformat_utc(started)
            row["ended_at"] = isoformat_utc(ended)
            row["duration_seconds"] = (ended - started).total_seconds()
            row["exit_code"] = exit_code
            row["jmh_summary"] = load_jmh_summary(result_path)
            log(f"  [JMH] exit={exit_code}  duree={row['duration_seconds']:.1f}s")

            if not args.skip_kwollect:
                log(f"  [Kwollect] Pause {args.kwollect_settle_seconds}s avant fetch ...")
                time.sleep(args.kwollect_settle_seconds)

                run_start = math.floor(started.timestamp())
                run_end = math.ceil(ended.timestamp())
                log(f"  [Kwollect] Fenetre run  : {run_start} -> {run_end} ({run_end - run_start}s)")
                log(f"  [Kwollect] Metrique : {','.join(args.metrics)}")
                run_url, bench_metrics = fetch_kwollect_metrics(
                    site=args.site,
                    node=args.node,
                    start_epoch=run_start,
                    end_epoch=run_end,
                    metrics=args.metrics,
                    job_id=args.job_id,
                )
                log(f"  [Kwollect] URL : {run_url}")
                log(f"  [Kwollect] {len(bench_metrics)} enregistrement(s) recu(s).")
                write_json(iter_dir / "kwollect-benchmark.json", bench_metrics)
                write_csv(iter_dir / "kwollect-benchmark.csv", bench_metrics)
                row["kwollect_benchmark_summary"] = compute_power_summary(bench_metrics)
                log(f"  [Kwollect] benchmark  -> {row['kwollect_benchmark_summary']}")

                if "idle_window" in row:
                    idle_start_epoch = math.floor(parse_timestamp(row["idle_window"]["start"]))
                    idle_end_epoch = math.ceil(parse_timestamp(row["idle_window"]["end"]))
                    log(f"  [Kwollect] Fenetre idle : {idle_start_epoch} -> {idle_end_epoch}")
                    idle_url, idle_metrics = fetch_kwollect_metrics(
                        site=args.site,
                        node=args.node,
                        start_epoch=idle_start_epoch,
                        end_epoch=idle_end_epoch,
                        metrics=args.metrics,
                        job_id=args.job_id,
                    )
                    log(f"  [Kwollect] URL : {idle_url}")
                    log(f"  [Kwollect] {len(idle_metrics)} enregistrement(s) recu(s).")
                    write_json(iter_dir / "kwollect-idle.json", idle_metrics)
                    write_csv(iter_dir / "kwollect-idle.csv", idle_metrics)
                    row["kwollect_idle_summary"] = compute_power_summary(idle_metrics)
                    log(f"  [Kwollect] idle        -> {row['kwollect_idle_summary']}")

                    run_energy = row["kwollect_benchmark_summary"].get("energy_j")
                    idle_avg_power = row["kwollect_idle_summary"].get("average_power_w")
                    if run_energy is not None and idle_avg_power is not None:
                        row["net_energy_j"] = run_energy - (idle_avg_power * row["duration_seconds"])
                        log(f"  [Kwollect] energie nette = {row['net_energy_j']:.2f} J")

            write_json(iter_dir / "run-summary.json", row)
            iteration_rows.append(row)
            if exit_code != 0:
                return exit_code

            if run_index < args.run_count and args.inter_iteration_seconds > 0:
                log(f"  [inter-iteration {args.inter_iteration_seconds}s ...]")
                time.sleep(args.inter_iteration_seconds)

        version_summary = summarize_version(version, iteration_rows)
        write_json(version_dir / "version-summary.json", version_summary)
        campaign_rows.append(version_summary)

        if version_index < len(args.versions) and args.rest_seconds > 0:
            log("")
            log(f"  [inter-version {args.rest_seconds}s ...]")
            time.sleep(args.rest_seconds)

    write_json(campaign_dir / "campaign-summary.json", campaign_rows)
    write_campaign_summary_csv(campaign_dir / "campaign-summary.csv", campaign_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
