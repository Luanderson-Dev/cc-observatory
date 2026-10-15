"""Summarize the memory of a compose project's containers, per service.

Each sample lists the project's running containers with ``docker ps`` (filtered by the
``com.docker.compose.project`` label, the service read from
``com.docker.compose.service``), reads one ``docker stats --no-stream`` and joins the
two by container ID. The summary has, per service, the usage in the last sample, the
peak over all samples and the memory limit, with percentages, plus a total whose peak
is the highest sum of one sample (not the sum of each service's peak, which may never
happen at once). Only the project's lines of ``docker stats`` are read, and a container
whose stats Docker could not read (``-- / --``) is left out of that sample. A sample
that fails once one has succeeded is skipped with a warning, and Ctrl+C prints the
summary so far. Run it from the repository root::

    uv run python -m tools.stack.memory --project ccobs-mem --samples 60 --interval 10

Usage is what ``docker stats`` reports: on cgroup v2, the container's memory minus its
inactive file cache, so active page cache counts. Docker prints it with four
significant digits, so the bytes here are rounded. A spike between two samples is
missed; ``--interval`` is the pause between them, and each ``docker stats`` reading
takes about two seconds more.

Developer tool, standard library only; not part of the published package.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from tools.capture.jsontypes import JSON, JSONObject

type Runner = Callable[[Sequence[str]], str]

DEFAULT_PROJECT: Final = "cc-observatory"
DEFAULT_SAMPLES: Final = 1
DEFAULT_INTERVAL: Final = 5.0
PROJECT_LABEL: Final = "com.docker.compose.project"
SERVICE_LABEL: Final = "com.docker.compose.service"
# One JSON object per container: its short ID and its compose service ("" if the label
# is missing). A template, because `--format json` joins every label into one string.
PS_TEMPLATE: Final = '{"id":{{json .ID}},"service":{{json (.Label "' + SERVICE_LABEL + '")}}}'
# Seconds a docker command may take.
COMMAND_TIMEOUT: Final = 60.0
# docker ps prints 12 hex digits of the ID, docker stats prints both forms.
SHORT_ID: Final = 12
MIB: Final = 1024**2
# Docker's size units: decimal (NetIO, BlockIO) and binary (MemUsage), from the
# go-units package it prints them with.
UNITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "pb": 1000**5,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
        "pib": 1024**5,
    }
)
# A number as Go's %.4g and %.3g print it (1e+03 included), then a unit.
SIZE: Final = re.compile(r"(?P<number>\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(?P<unit>[a-z]+)", re.I)
TOTAL: Final = "total"
# What docker stats prints for a size it could not read.
NO_READING: Final = "--"


class MemoryToolError(Exception):
    """A docker command that could not run or failed."""


# Parsing.


@dataclass(frozen=True)
class Usage:
    """One reading of a container (or a service): bytes used and the limit (0: none)."""

    used: int
    limit: int

    @property
    def percent(self) -> float | None:
        """``used`` as a percentage of ``limit``; None without a limit."""
        return _percent(self.used, self.limit)


def _percent(part: int, whole: int) -> float | None:
    return 100 * part / whole if whole else None


def parse_size(text: str) -> int:
    """A size as Docker prints it (``226.4MiB``, ``1.2GiB``, ``512kB``, ``0B``) in
    bytes, rounded. Raises ``ValueError`` on anything else."""
    match = SIZE.fullmatch(text.strip())
    factor = UNITS.get(match["unit"].lower()) if match else None
    if match is None or factor is None:
        raise ValueError(f"{text!r} is not a docker size")
    return round(float(match["number"]) * factor)


def parse_mem_usage(text: str) -> Usage:
    """``docker stats``'s MemUsage (``226.4MiB / 384MiB``); a size alone (Windows
    containers) has no limit. Raises ``ValueError`` on anything else."""
    used, slash, limit = text.partition("/")
    if "/" in limit:
        raise ValueError(f"{text!r} is not a docker memory usage (size / size)")
    return Usage(parse_size(used), parse_size(limit) if slash else 0)


def _json_lines(output: str, command: str) -> list[tuple[int, JSONObject]]:
    """The JSON objects of ``output``, one per non-blank line, with their line number."""
    objects: list[tuple[int, JSONObject]] = []
    for number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            document: JSON = json.loads(line)
        except ValueError:
            document = None
        if not isinstance(document, dict):
            raise ValueError(f"{command} line {number} is not a JSON object: {line[:200]!r}")
        objects.append((number, document))
    return objects


def parse_containers(output: str) -> dict[str, str]:
    """The service of each container in ``docker ps`` output (``ps_arguments``'s
    template), by short ID. Containers without a service label are left out. Raises
    ``ValueError`` on a line that is not such an object."""
    containers: dict[str, str] = {}
    for number, entry in _json_lines(output, "docker ps"):
        container_id, service = entry.get("id"), entry.get("service")
        if not isinstance(container_id, str) or not isinstance(service, str):
            raise ValueError(f"docker ps line {number} has no id and service: {entry!r}")
        if service:
            containers[container_id[:SHORT_ID]] = service
    return containers


def _no_reading(mem_usage: str) -> bool:
    """Whether MemUsage is Docker's ``-- / --`` (``--`` for Windows containers): no valid
    stats for the container in this reading."""
    return all(part.strip() == NO_READING for part in mem_usage.split("/"))


def parse_stats(output: str, wanted: Collection[str]) -> dict[str, Usage]:
    """The memory of each container of ``wanted`` (short IDs, as ``parse_containers``
    returns them) in ``docker stats --format json`` output, by short ID.

    docker stats lists every container on the host, so the lines of other containers are
    skipped unread. A wanted container whose stats Docker could not read (MemUsage
    ``-- / --``: it is exiting, or the daemon timed out waiting for its stats) is left
    out of this reading, as ``join`` leaves out a container with no stats. Raises
    ``ValueError`` on a line that is not a JSON object with an ID, which could be a
    wanted container's, and on a wanted container's line without a readable MemUsage."""
    usages: dict[str, Usage] = {}
    for number, entry in _json_lines(output, "docker stats"):
        container_id = entry.get("ID")
        if not isinstance(container_id, str):
            raise ValueError(f"docker stats line {number} has no ID: {entry!r}")
        short_id = container_id[:SHORT_ID]
        if short_id not in wanted:
            continue
        mem_usage = entry.get("MemUsage")
        if not isinstance(mem_usage, str):
            raise ValueError(f"docker stats line {number} has no MemUsage: {entry!r}")
        if _no_reading(mem_usage):
            continue
        try:
            usages[short_id] = parse_mem_usage(mem_usage)
        except ValueError as error:
            raise ValueError(f"docker stats line {number}: {error}") from None
    return usages


def join(containers: Mapping[str, str], stats: Mapping[str, Usage]) -> dict[str, Usage]:
    """The usage of each service: the stats of its containers (by short ID), added up
    when a service has several. Stats of other containers are left out, and so is a
    container with no stats (it stopped in between)."""
    services: dict[str, Usage] = {}
    for container_id, service in containers.items():
        usage = stats.get(container_id)
        if usage is None:
            continue
        before = services.get(service, Usage(0, 0))
        services[service] = Usage(before.used + usage.used, before.limit + usage.limit)
    return services


# The summary.


@dataclass(frozen=True)
class ServiceMemory:
    """One line of the summary: the usage in the last sample (0 if the service was not
    running then), the peak over every sample and the last known limit (0: none)."""

    service: str
    used: int
    peak: int
    limit: int

    @property
    def used_percent(self) -> float | None:
        return _percent(self.used, self.limit)

    @property
    def peak_percent(self) -> float | None:
        return _percent(self.peak, self.limit)


@dataclass(frozen=True)
class Summary:
    """The services, sorted by name, and their total."""

    services: tuple[ServiceMemory, ...]
    total: ServiceMemory
    samples: int


def summarize(samples: Sequence[Mapping[str, Usage]]) -> Summary:
    """Per service, the usage in the last sample, the peak and the last limit above 0
    (a restarting container reads 0B / 0B); the total's peak is the highest sum of one
    sample."""
    peaks: dict[str, int] = {}
    limits: dict[str, int] = {}
    for reading in samples:
        for service, usage in reading.items():
            peaks[service] = max(peaks.get(service, 0), usage.used)
            limits[service] = usage.limit or limits.get(service, 0)
    last = samples[-1] if samples else {}
    services = tuple(
        ServiceMemory(
            service,
            used=last[service].used if service in last else 0,
            peak=peaks[service],
            limit=limits[service],
        )
        for service in sorted(peaks)
    )
    total = ServiceMemory(
        TOTAL,
        used=sum(service.used for service in services),
        peak=max((sum(usage.used for usage in reading.values()) for reading in samples), default=0),
        limit=sum(service.limit for service in services),
    )
    return Summary(services, total, len(samples))


# Formatting.


def format_size(size: int) -> str:
    """Bytes in MiB with one decimal (``226.4MiB``), so services compare at a glance."""
    return f"{size / MIB:.1f}MiB"


def format_percent(percent: float | None) -> str:
    return "-" if percent is None else f"{percent:.1f}%"


def _row(*cells: str) -> str:
    service, used, peak, limit, used_percent, peak_percent = cells
    return f"{service:<14}{used:>11}{peak:>11}{limit:>11}{used_percent:>8}{peak_percent:>8}"


def _line(item: ServiceMemory) -> str:
    return _row(
        item.service,
        format_size(item.used),
        format_size(item.peak),
        format_size(item.limit) if item.limit else "-",
        format_percent(item.used_percent),
        format_percent(item.peak_percent),
    )


def format_table(summary: Summary, *, project: str, interval: float) -> str:
    """A text table: a heading, one line per service and the total."""
    heading = f"{project}: {summary.samples} sample"
    if summary.samples > 1:
        heading += f"s, {interval:.1f} s apart"
    lines = [heading, _row("SERVICE", "USED", "PEAK", "LIMIT", "USED%", "PEAK%")]
    lines += [_line(item) for item in (*summary.services, summary.total)]
    return "\n".join(lines)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _item_json(item: ServiceMemory) -> JSONObject:
    return {
        "service": item.service,
        "used_bytes": item.used,
        "peak_bytes": item.peak,
        "limit_bytes": item.limit or None,
        "used_mib": round(item.used / MIB, 1),
        "peak_mib": round(item.peak / MIB, 1),
        "limit_mib": round(item.limit / MIB, 1) if item.limit else None,
        "used_percent": _rounded(item.used_percent),
        "peak_percent": _rounded(item.peak_percent),
    }


def to_json(summary: Summary, *, project: str, interval: float) -> JSONObject:
    """The summary as a JSON object: bytes, MiB and percentages (None without a limit),
    and the interval (None with a single sample, as the table leaves it out)."""
    return {
        "project": project,
        "samples": summary.samples,
        "interval_s": interval if summary.samples > 1 else None,
        "services": [_item_json(item) for item in summary.services],
        "total": _item_json(summary.total),
    }


# Docker.


def ps_arguments(project: str) -> list[str]:
    """``docker ps`` arguments that list the running containers of ``project``."""
    return ["ps", "--filter", f"label={PROJECT_LABEL}={project}", "--format", PS_TEMPLATE]


def stats_arguments() -> list[str]:
    """``docker stats`` arguments for one reading of every running container."""
    return ["stats", "--no-stream", "--format", "json"]


def run_docker(arguments: Sequence[str]) -> str:
    """Run ``docker`` with ``arguments`` and return its output; raises
    ``MemoryToolError`` when docker is missing, cannot run or exits with an error."""
    docker = shutil.which("docker")
    if docker is None:
        raise MemoryToolError("docker is not on PATH")
    name = f"docker {arguments[0]}"
    try:
        # S603 cannot see that this is safe (ruff only trusts literals): docker comes from
        # shutil.which; the only outside value, the project name, sits inside the --filter
        # argument of an argument list, and no shell runs.
        result = subprocess.run(  # noqa: S603
            [docker, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MemoryToolError(f"`{name}` failed: {error}") from error
    if result.returncode != 0:
        raise MemoryToolError(
            f"`{name}` exited with {result.returncode}: {result.stderr.strip()[:500]}"
        )
    return result.stdout


def sample(project: str, runner: Runner) -> dict[str, Usage]:
    """One reading of ``project``'s running containers, by service."""
    containers = parse_containers(runner(ps_arguments(project)))
    if not containers:
        return {}
    return join(containers, parse_stats(runner(stats_arguments()), containers.keys()))


# Command line.


@dataclass(frozen=True)
class Options:
    project: str
    samples: int
    interval: float
    json: bool


def _project(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("the project name is empty")
    return text.strip()


def _samples(text: str) -> int:
    try:
        count = int(text)
    except ValueError:
        count = 0
    if count < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of 1 or more")
    return count


def _interval(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds, 0 or more")
    return seconds


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        prog="python -m tools.stack.memory",
        description="Memory of a compose project's containers, per service.",
    )
    parser.add_argument(
        "--project",
        type=_project,
        default=DEFAULT_PROJECT,
        help=f"compose project name ({DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--samples",
        type=_samples,
        default=DEFAULT_SAMPLES,
        help=f"readings to take, keeping each service's peak ({DEFAULT_SAMPLES})",
    )
    parser.add_argument(
        "--interval",
        type=_interval,
        default=DEFAULT_INTERVAL,
        help=f"seconds between two readings ({DEFAULT_INTERVAL})",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    return Options(args.project, args.samples, args.interval, args.json)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = run_docker,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Sample the project and print the summary of the samples taken.

    A sample that fails once one has succeeded is reported on stderr and skipped, and
    Ctrl+C stops the sampling and prints the summary so far, so a long run keeps what it
    measured. The result is 1, with a line on stderr, when no sample succeeds: docker
    fails at the first one, or the project has no running container."""
    options = parse_args(argv)
    samples: list[dict[str, Usage]] = []
    try:
        for index in range(options.samples):
            if index:
                sleep(options.interval)
            try:
                samples.append(sample(options.project, runner))
            except (MemoryToolError, ValueError) as error:
                if not samples:
                    print(f"memory: {error}", file=sys.stderr)
                    return 1
                where = f"sample {index + 1} of {options.samples}"
                print(f"memory: {where} skipped: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        taken = f"{len(samples)} sample{'' if len(samples) == 1 else 's'}"
        print(f"memory: interrupted after {taken}", file=sys.stderr)
        if not samples:
            return 1
    summary = summarize(samples)
    if not summary.services:
        print(f"memory: no running container in compose project {options.project}", file=sys.stderr)
        return 1
    if options.json:
        document = to_json(summary, project=options.project, interval=options.interval)
        print(json.dumps(document, indent=2))
    else:
        print(format_table(summary, project=options.project, interval=options.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
