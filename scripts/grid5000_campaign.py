#!/usr/bin/env python3
"""Run an Eclipse Collections JMH version matrix and fetch Kwollect metrics."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


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

    return datetime.fromisoformat(candidate).timestamp()


def slugify(value: str) -> str:
    chars = []
    for char in value:
        chars.append(char if char.isalnum() or char in ("-", "_", ".") else "_")
    return "".join(chars)


def short_hostname() -> str:
    return socket.gethostname().split(".")[0]


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
        delta = max(0.0, t1 - t0)
        area_j += ((p0 + p1) / 2.0) * delta

    duration = max(0.0, samples[-1][0] - samples[0][0])
    average_power = area_j / duration if duration > 0 else samples[0][1]

    return {
        "samples": len(samples),
        "duration_seconds": duration,
        "average_power_w": average_power,
        "peak_power_w": peak_power,
        "energy_j": area_j,
    }


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


def fetch_kwollect_metrics(
    *,
    site: str,
    node: str,
    start_epoch: int,
    end_epoch: int,
    metrics: list[str],
    job_id: str | None,
) -> list[dict[str, Any]]:
    query = {
        "nodes": node,
        "start_time": str(start_epoch),
        "end_time": str(end_epoch),
        "metrics": ",".join(metrics),
    }
    if job_id:
        query["job_id"] = job_id

    url = (
        f"https://api.grid5000.fr/stable/sites/{site}/metrics?"
        f"{urllib.parse.urlencode(query)}"
    )

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def build_gradle_command(args: argparse.Namespace, version: str, run_dir: Path) -> list[str]:
    return [
        args.gradle_command,
        "--no-daemon",
        "--rerun-tasks",
        "jmhJar",
        f"-PecVersion={version}",
    ]


def build_jmh_command(args: argparse.Namespace, run_dir: Path) -> list[str]:
    result_extension = args.jmh_result_format.lower()
    jar_path = REPO_ROOT / "build" / "libs" / "jmh-eclipse-benchmark-jmh.jar"

    command = [
        args.java_command,
        "-jar",
        str(jar_path),
        args.includes or ".*",
        "-rf",
        args.jmh_result_format,
        "-rff",
        str(run_dir / f"jmh-results.{result_extension}"),
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

    return command


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run JMH benchmarks for multiple Eclipse Collections versions and "
            "collect Kwollect metrics for each run."
        )
    )
    parser.add_argument(
        "--versions",
        nargs="+",
        required=True,
        help="List of Eclipse Collections versions to benchmark.",
    )
    parser.add_argument(
        "--site",
        required=True,
        help="Grid5000 site name, for example lyon.",
    )
    parser.add_argument(
        "--node",
        default=short_hostname(),
        help="Reserved node name. Defaults to the current host.",
    )
    parser.add_argument(
        "--job-id",
        default=os.environ.get("OAR_JOB_ID"),
        help="Optional OAR job id used to restrict Kwollect queries.",
    )
    parser.add_argument(
        "--metrics",
        nargs="+",
        default=["wattmetre_power_watt"],
        help="Kwollect metrics to fetch.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Campaign output directory. Defaults to build/campaigns/<timestamp>.",
    )
    parser.add_argument(
        "--gradle-command",
        default="./gradlew" if os.name != "nt" else "gradlew.bat",
        help="Gradle wrapper command to execute.",
    )
    parser.add_argument(
        "--java-command",
        default="java",
        help="Java command used to launch the generated JMH jar.",
    )
    parser.add_argument(
        "--includes",
        default=None,
        help="Optional JMH include regex.",
    )
    parser.add_argument(
        "--excludes",
        default=None,
        help="Optional JMH exclude regex.",
    )
    parser.add_argument(
        "--benchmark-param",
        action="append",
        default=[],
        help="Optional JMH benchmark parameter, for example size=1000,10000.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="JMH measurement iterations.",
    )
    parser.add_argument(
        "--warmup-iterations",
        type=int,
        default=5,
        help="JMH warmup iterations.",
    )
    parser.add_argument(
        "--forks",
        type=int,
        default=2,
        help="JMH forks per benchmark.",
    )
    parser.add_argument(
        "--iteration-time",
        default="1s",
        help="JMH measurement time per iteration.",
    )
    parser.add_argument(
        "--warmup-time",
        default="1s",
        help="JMH warmup time per iteration.",
    )
    parser.add_argument(
        "--time-unit",
        default="ms",
        help="JMH time unit.",
    )
    parser.add_argument(
        "--benchmark-mode",
        default="avgt",
        help="JMH benchmark mode.",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=0,
        help="Optional idle window collected before each version run.",
    )
    parser.add_argument(
        "--rest-seconds",
        type=int,
        default=5,
        help="Rest time after each benchmark run.",
    )
    parser.add_argument(
        "--kwollect-settle-seconds",
        type=int,
        default=10,
        help="Delay before fetching Kwollect metrics after a run.",
    )
    parser.add_argument(
        "--skip-kwollect",
        action="store_true",
        help="Skip Kwollect collection. Useful for local smoke tests.",
    )
    parser.add_argument(
        "--jmh-result-format",
        default="JSON",
        choices=["JSON", "CSV", "TEXT", "SCSV", "NONE"],
        help="JMH machine-readable output format.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    started_at = utc_now()
    default_output_dir = (
        REPO_ROOT
        / "build"
        / "campaigns"
        / started_at.strftime("%Y%m%dT%H%M%SZ")
    )
    campaign_dir = Path(args.output_dir) if args.output_dir else default_output_dir
    campaign_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "started_at": isoformat_utc(started_at),
        "repo_root": str(REPO_ROOT),
        "site": args.site,
        "node": args.node,
        "job_id": args.job_id,
        "metrics": args.metrics,
        "versions": args.versions,
        "runs": [],
    }

    write_json(campaign_dir / "campaign-manifest.json", manifest)

    for version in args.versions:
        version_slug = slugify(version)
        run_dir = campaign_dir / version_slug
        run_dir.mkdir(parents=True, exist_ok=True)

        run_record: dict[str, Any] = {
            "version": version,
            "node": args.node,
            "site": args.site,
            "job_id": args.job_id,
            "metrics": args.metrics,
        }

        if args.idle_seconds > 0:
            idle_start = utc_now()
            time.sleep(args.idle_seconds)
            idle_end = utc_now()
            run_record["idle_window"] = {
                "start": isoformat_utc(idle_start),
                "end": isoformat_utc(idle_end),
                "duration_seconds": args.idle_seconds,
            }

        started = utc_now()
        gradle_command = build_gradle_command(args, version, run_dir)
        run_record["gradle_command"] = gradle_command
        gradle_exit_code = run_process(
            gradle_command,
            REPO_ROOT,
            run_dir / "gradle-build.log",
        )

        if gradle_exit_code != 0:
            ended = utc_now()
            run_record["started_at"] = isoformat_utc(started)
            run_record["ended_at"] = isoformat_utc(ended)
            run_record["duration_seconds"] = (ended - started).total_seconds()
            run_record["exit_code"] = gradle_exit_code
            write_json(run_dir / "run-summary.json", run_record)
            manifest["runs"].append(run_record)
            write_json(campaign_dir / "campaign-manifest.json", manifest)
            return gradle_exit_code

        jmh_command = build_jmh_command(args, run_dir)
        run_record["jmh_command"] = jmh_command
        exit_code = run_process(jmh_command, REPO_ROOT, run_dir / "jmh-run.log")
        ended = utc_now()

        run_record["started_at"] = isoformat_utc(started)
        run_record["ended_at"] = isoformat_utc(ended)
        run_record["duration_seconds"] = (ended - started).total_seconds()
        run_record["exit_code"] = exit_code

        if args.rest_seconds > 0:
            time.sleep(args.rest_seconds)

        if not args.skip_kwollect:
            time.sleep(args.kwollect_settle_seconds)

            try:
                bench_metrics = fetch_kwollect_metrics(
                    site=args.site,
                    node=args.node,
                    start_epoch=math.floor(started.timestamp()),
                    end_epoch=math.ceil(ended.timestamp()),
                    metrics=args.metrics,
                    job_id=args.job_id,
                )
                write_json(run_dir / "kwollect-benchmark.json", bench_metrics)
                write_csv(run_dir / "kwollect-benchmark.csv", bench_metrics)
                run_record["kwollect_benchmark_summary"] = compute_power_summary(bench_metrics)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                run_record["kwollect_benchmark_error"] = str(exc)

            if "idle_window" in run_record:
                idle_window = run_record["idle_window"]
                try:
                    idle_metrics = fetch_kwollect_metrics(
                        site=args.site,
                        node=args.node,
                        start_epoch=math.floor(parse_timestamp(idle_window["start"])),
                        end_epoch=math.ceil(parse_timestamp(idle_window["end"])),
                        metrics=args.metrics,
                        job_id=args.job_id,
                    )
                    write_json(run_dir / "kwollect-idle.json", idle_metrics)
                    write_csv(run_dir / "kwollect-idle.csv", idle_metrics)
                    idle_summary = compute_power_summary(idle_metrics)
                    run_record["kwollect_idle_summary"] = idle_summary

                    benchmark_summary = run_record.get("kwollect_benchmark_summary", {})
                    run_energy = benchmark_summary.get("energy_j")
                    idle_avg_power = idle_summary.get("average_power_w")
                    duration = run_record["duration_seconds"]
                    if run_energy is not None and idle_avg_power is not None:
                        run_record["net_energy_j"] = run_energy - (idle_avg_power * duration)
                except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                    run_record["kwollect_idle_error"] = str(exc)

        write_json(run_dir / "run-summary.json", run_record)
        manifest["runs"].append(run_record)
        write_json(campaign_dir / "campaign-manifest.json", manifest)

        if exit_code != 0:
            return exit_code

    manifest["ended_at"] = isoformat_utc(utc_now())
    write_json(campaign_dir / "campaign-manifest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
