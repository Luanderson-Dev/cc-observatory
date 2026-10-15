"""Tests for the memory summary of a compose project (``tools/stack/memory.py``).

Pure unit tests: no Docker. The ``docker ps`` and ``docker stats`` lines below keep the
shape that Docker 29.8 prints (field names, size units and four significant digits),
with made-up container IDs; every subprocess call is faked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import Final

import pytest

from tools.stack.memory import (
    DEFAULT_INTERVAL,
    DEFAULT_PROJECT,
    MemoryToolError,
    ServiceMemory,
    Summary,
    Usage,
    format_percent,
    format_size,
    format_table,
    join,
    main,
    parse_args,
    parse_containers,
    parse_mem_usage,
    parse_size,
    parse_stats,
    ps_arguments,
    run_docker,
    sample,
    stats_arguments,
    summarize,
    to_json,
)

KIB: Final = 1024
MIB: Final = 1024**2
GIB: Final = 1024**3

LOKI_ID: Final = "0123456789ab"
GRAFANA_ID: Final = "abcdef012345"
OTHER_ID: Final = "fedcba987654"


def ps_line(container_id: str, service: str) -> str:
    """One line of ``docker ps --format`` with ``ps_arguments``'s template."""
    return json.dumps({"id": container_id, "service": service})


def stats_line(container_id: str, name: str, mem_usage: str) -> str:
    """One line of ``docker stats --no-stream --format json``, as Docker 29.8 prints it
    with no container argument (``Container`` is then the full ID)."""
    return json.dumps(
        {
            "BlockIO": "0B / 41kB",
            "CPUPerc": "0.36%",
            "Container": container_id + "0" * 52,
            "ID": container_id,
            "MemPerc": "12.92%",
            "MemUsage": mem_usage,
            "Name": name,
            "NetIO": "99.6kB / 275kB",
            "PIDs": "10",
        }
    )


# Sizes.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0B", 0),
        ("512B", 512),
        ("512kB", 512_000),
        ("1.5MB", 1_500_000),
        ("2GB", 2_000_000_000),
        ("1TB", 10**12),
        ("12KiB", 12 * KIB),
        ("384MiB", 384 * MIB),
        ("226.4MiB", round(226.4 * MIB)),
        ("73.85MiB", round(73.85 * MIB)),
        ("1.2GiB", round(1.2 * GIB)),
        ("3GiB", 3 * GIB),
        ("2TiB", 2 * 1024**4),
        # %.3g, which Docker uses for decimal sizes, prints 999.95 as 1e+03.
        ("1e+03kB", 1_000_000),
        (" 384MiB ", 384 * MIB),
        ("1.5 MiB", round(1.5 * MIB)),
    ],
)
def test_parse_size_reads_docker_units(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "MiB", "12", "12XB", "-1MiB", "1.2.3MiB", "--", "1 2MiB"])
def test_parse_size_rejects_what_is_not_a_size(text: str) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_size(text)


def test_parse_mem_usage_reads_usage_and_limit() -> None:
    assert parse_mem_usage("226.4MiB / 384MiB") == Usage(round(226.4 * MIB), 384 * MIB)


def test_parse_mem_usage_of_a_stopped_container_has_no_limit() -> None:
    usage = parse_mem_usage("0B / 0B")

    assert usage == Usage(0, 0)
    assert usage.percent is None


def test_parse_mem_usage_without_a_limit() -> None:
    # Windows containers report only the private working set.
    assert parse_mem_usage("1.5MiB") == Usage(round(1.5 * MIB), 0)


@pytest.mark.parametrize("text", ["abc / 384MiB", "1MiB / 2MiB / 3MiB", "", " / "])
def test_parse_mem_usage_rejects_other_text(text: str) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_mem_usage(text)


def test_usage_percent() -> None:
    assert Usage(192 * MIB, 384 * MIB).percent == 50.0


# docker ps and docker stats.


def test_ps_arguments_filter_by_the_compose_project_label() -> None:
    arguments = ps_arguments("ccobs-mem")

    assert arguments[0] == "ps"
    assert (
        arguments[arguments.index("--filter") + 1] == "label=com.docker.compose.project=ccobs-mem"
    )
    template = arguments[arguments.index("--format") + 1]
    assert '.Label "com.docker.compose.service"' in template
    assert "--all" not in arguments


def test_stats_arguments_take_one_reading_as_json() -> None:
    assert stats_arguments() == ["stats", "--no-stream", "--format", "json"]


def test_parse_containers_maps_short_ids_to_services() -> None:
    output = "\n".join(
        [
            ps_line(LOKI_ID, "loki"),
            "",
            ps_line(GRAFANA_ID + "f" * 52, "grafana"),
            # A container with the project label but no service label: not a service.
            ps_line(OTHER_ID, ""),
        ]
    )

    assert parse_containers(output + "\n") == {LOKI_ID: "loki", GRAFANA_ID: "grafana"}


def test_parse_containers_of_no_output_is_empty() -> None:
    assert parse_containers("") == {}


@pytest.mark.parametrize(
    "line", ["not json", "[1, 2]", '{"service": "loki"}', '{"id": 12, "service": "loki"}']
)
def test_parse_containers_rejects_other_lines(line: str) -> None:
    with pytest.raises(ValueError, match="docker ps"):
        parse_containers(line)


def test_parse_stats_maps_short_ids_to_usage() -> None:
    output = "\n".join(
        [
            stats_line(LOKI_ID, "ccobs-mem-loki-1", "73.85MiB / 512MiB"),
            "",
            stats_line(GRAFANA_ID, "ccobs-mem-grafana-1", "370.8MiB / 384MiB"),
        ]
    )

    assert parse_stats(output, {LOKI_ID, GRAFANA_ID}) == {
        LOKI_ID: Usage(round(73.85 * MIB), 512 * MIB),
        GRAFANA_ID: Usage(round(370.8 * MIB), 384 * MIB),
    }


@pytest.mark.parametrize("mem_usage", ["-- / --", "--"])
def test_parse_stats_leaves_out_a_project_container_without_a_reading(mem_usage: str) -> None:
    # Docker prints -- when a container's stats are invalid: it is exiting, or the daemon
    # timed out waiting for them, which a busy Docker Desktop does at the peaks measured.
    output = "\n".join(
        [
            stats_line(LOKI_ID, "ccobs-mem-loki-1", mem_usage),
            stats_line(GRAFANA_ID, "ccobs-mem-grafana-1", "370.8MiB / 384MiB"),
        ]
    )

    assert parse_stats(output, {LOKI_ID, GRAFANA_ID}) == {
        GRAFANA_ID: Usage(round(370.8 * MIB), 384 * MIB)
    }


@pytest.mark.parametrize(
    "mem_usage", ["-- / --", "lots / 3GiB", 5, None], ids=["no-reading", "bad", "number", "none"]
)
def test_parse_stats_ignores_the_lines_of_other_containers(mem_usage: str | int | None) -> None:
    # docker stats lists every container on the host; another project's line is not
    # this project's business, whatever it holds.
    foreign = json.dumps({"ID": OTHER_ID, "Name": "other-postgres-1", "MemUsage": mem_usage})
    output = "\n".join([foreign, stats_line(LOKI_ID, "ccobs-mem-loki-1", "73.85MiB / 512MiB")])

    assert parse_stats(output, {LOKI_ID}) == {LOKI_ID: Usage(round(73.85 * MIB), 512 * MIB)}


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '"text"',
        '{"MemUsage": "1MiB / 2MiB"}',
        '{"ID": "0123456789ab"}',
        '{"ID": "0123456789ab", "MemUsage": 5}',
    ],
)
def test_parse_stats_rejects_other_lines(line: str) -> None:
    # A line that is not JSON or has no ID could be the project's own.
    with pytest.raises(ValueError, match="docker stats"):
        parse_stats(line, {LOKI_ID})


def test_parse_stats_names_the_line_with_a_bad_size() -> None:
    with pytest.raises(ValueError, match=r"docker stats.*size"):
        parse_stats(stats_line(LOKI_ID, "ccobs-mem-loki-1", "lots / 512MiB"), {LOKI_ID})


def test_join_keeps_the_project_containers_by_service() -> None:
    containers = {LOKI_ID: "loki", GRAFANA_ID: "grafana"}
    stats = {
        LOKI_ID: Usage(70 * MIB, 512 * MIB),
        GRAFANA_ID: Usage(200 * MIB, 384 * MIB),
        # Another project's container, which docker stats lists too.
        OTHER_ID: Usage(100 * MIB, 3 * GIB),
    }

    assert join(containers, stats) == {
        "loki": Usage(70 * MIB, 512 * MIB),
        "grafana": Usage(200 * MIB, 384 * MIB),
    }


def test_join_skips_a_container_that_stopped_before_docker_stats() -> None:
    assert join({LOKI_ID: "loki"}, {}) == {}


def test_join_adds_up_the_containers_of_a_scaled_service() -> None:
    containers = {LOKI_ID: "loki", OTHER_ID: "loki"}
    stats = {LOKI_ID: Usage(70 * MIB, 512 * MIB), OTHER_ID: Usage(30 * MIB, 512 * MIB)}

    assert join(containers, stats) == {"loki": Usage(100 * MIB, 1024 * MIB)}


# The summary.


def test_summarize_keeps_the_last_usage_and_the_peak_of_each_service() -> None:
    samples = [
        {"loki": Usage(60 * MIB, 512 * MIB), "grafana": Usage(300 * MIB, 384 * MIB)},
        {"loki": Usage(90 * MIB, 512 * MIB), "grafana": Usage(250 * MIB, 384 * MIB)},
        {"loki": Usage(80 * MIB, 512 * MIB), "grafana": Usage(260 * MIB, 384 * MIB)},
    ]

    summary = summarize(samples)

    assert summary.samples == 3
    assert summary.services == (
        ServiceMemory("grafana", used=260 * MIB, peak=300 * MIB, limit=384 * MIB),
        ServiceMemory("loki", used=80 * MIB, peak=90 * MIB, limit=512 * MIB),
    )
    # The total peak is the highest sum of one sample (360 MiB, then 340, then 340), not
    # the sum of each service's peak (390 MiB), which never happened at once.
    assert summary.total == ServiceMemory("total", used=340 * MIB, peak=360 * MIB, limit=896 * MIB)


def test_summarize_counts_a_service_that_appears_later() -> None:
    # During a first start the services come up one by one.
    samples = [
        {"loki": Usage(40 * MIB, 512 * MIB)},
        {"loki": Usage(45 * MIB, 512 * MIB), "tempo": Usage(20 * MIB, 512 * MIB)},
        {"loki": Usage(50 * MIB, 512 * MIB)},
    ]

    services = {service.service: service for service in summarize(samples).services}

    assert services["tempo"] == ServiceMemory("tempo", used=0, peak=20 * MIB, limit=512 * MIB)
    assert services["loki"] == ServiceMemory("loki", used=50 * MIB, peak=50 * MIB, limit=512 * MIB)


def test_summarize_keeps_the_last_known_limit() -> None:
    # A restarting container reads 0B / 0B for a moment.
    samples = [{"loki": Usage(60 * MIB, 512 * MIB)}, {"loki": Usage(0, 0)}]

    (loki,) = summarize(samples).services

    assert loki == ServiceMemory("loki", used=0, peak=60 * MIB, limit=512 * MIB)


def test_summarize_of_no_containers_is_empty() -> None:
    summary = summarize([{}, {}])

    assert summary.services == ()
    assert summary.total == ServiceMemory("total", used=0, peak=0, limit=0)


def test_service_memory_percentages() -> None:
    grafana = ServiceMemory("grafana", used=192 * MIB, peak=288 * MIB, limit=384 * MIB)

    assert grafana.used_percent == 50.0
    assert grafana.peak_percent == 75.0
    unlimited = ServiceMemory("app", used=MIB, peak=MIB, limit=0)
    assert unlimited.used_percent is None
    assert unlimited.peak_percent is None


# Formatting.


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0.0MiB"),
        (round(226.4 * MIB), "226.4MiB"),
        (3 * GIB, "3072.0MiB"),
        (512 * KIB, "0.5MiB"),
    ],
)
def test_format_size_in_mib(size: int, expected: str) -> None:
    assert format_size(size) == expected


@pytest.mark.parametrize(
    ("percent", "expected"), [(None, "-"), (59.04, "59.0%"), (100.0, "100.0%")]
)
def test_format_percent(percent: float | None, expected: str) -> None:
    assert format_percent(percent) == expected


SUMMARY: Final = Summary(
    services=(
        ServiceMemory("grafana", used=round(226.4 * MIB), peak=round(230.1 * MIB), limit=384 * MIB),
        ServiceMemory("otel-collector", used=42 * MIB, peak=50 * MIB, limit=256 * MIB),
        ServiceMemory("app", used=10 * MIB, peak=12 * MIB, limit=0),
    ),
    total=ServiceMemory("total", used=round(278.4 * MIB), peak=round(290.1 * MIB), limit=640 * MIB),
    samples=3,
)


def test_format_table() -> None:
    table = format_table(SUMMARY, project="ccobs-mem", interval=5.0)

    assert table.splitlines() == [
        "ccobs-mem: 3 samples, 5.0 s apart",
        "SERVICE              USED       PEAK      LIMIT   USED%   PEAK%",
        "grafana          226.4MiB   230.1MiB   384.0MiB   59.0%   59.9%",
        "otel-collector    42.0MiB    50.0MiB   256.0MiB   16.4%   19.5%",
        "app               10.0MiB    12.0MiB          -       -       -",
        "total            278.4MiB   290.1MiB   640.0MiB   43.5%   45.3%",
    ]


def test_format_table_of_one_sample() -> None:
    summary = Summary(services=(), total=ServiceMemory("total", 0, 0, 0), samples=1)

    assert format_table(summary, project="cc-observatory", interval=5.0).splitlines()[0] == (
        "cc-observatory: 1 sample"
    )


def test_to_json() -> None:
    document = to_json(SUMMARY, project="ccobs-mem", interval=5.0)

    assert document["project"] == "ccobs-mem"
    assert document["samples"] == 3
    assert document["interval_s"] == 5.0
    services = document["services"]
    assert isinstance(services, list)
    assert [item["service"] for item in services if isinstance(item, dict)] == [
        "grafana",
        "otel-collector",
        "app",
    ]
    assert services[0] == {
        "service": "grafana",
        "used_bytes": round(226.4 * MIB),
        "peak_bytes": round(230.1 * MIB),
        "limit_bytes": 384 * MIB,
        "used_mib": 226.4,
        "peak_mib": 230.1,
        "limit_mib": 384.0,
        "used_percent": 59.0,
        "peak_percent": 59.9,
    }
    assert services[2] == {
        "service": "app",
        "used_bytes": 10 * MIB,
        "peak_bytes": 12 * MIB,
        "limit_bytes": None,
        "used_mib": 10.0,
        "peak_mib": 12.0,
        "limit_mib": None,
        "used_percent": None,
        "peak_percent": None,
    }
    total = document["total"]
    assert isinstance(total, dict)
    assert total["service"] == "total"
    assert total["peak_mib"] == 290.1
    json.dumps(document)


def test_to_json_of_one_sample_has_no_interval() -> None:
    # As in the table, which names the interval only between two samples or more.
    summary = Summary(services=(), total=ServiceMemory("total", 0, 0, 0), samples=1)

    assert to_json(summary, project="cc-observatory", interval=5.0)["interval_s"] is None


# Command line arguments.


def test_parse_args_defaults() -> None:
    args = parse_args([])

    assert args.project == DEFAULT_PROJECT == "cc-observatory"
    assert args.samples == 1
    assert args.interval == DEFAULT_INTERVAL
    assert args.json is False


def test_parse_args_reads_every_option() -> None:
    args = parse_args(["--project", "ccobs-mem", "--samples", "12", "--interval", "2.5", "--json"])

    assert (args.project, args.samples, args.interval, args.json) == ("ccobs-mem", 12, 2.5, True)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--samples", "0"], "--samples"),
        (["--samples", "two"], "--samples"),
        (["--interval", "-1"], "--interval"),
        (["--interval", "nan"], "--interval"),
        (["--interval", "inf"], "--interval"),
        (["--interval", "soon"], "--interval"),
        (["--project", ""], "--project"),
        (["--project", "  "], "--project"),
    ],
)
def test_parse_args_rejects_bad_values(
    arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(arguments)

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_help_names_the_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for option in ("--project", "--samples", "--interval", "--json"):
        assert option in out


# Sampling and the command line, with docker faked.


type Answer = str | BaseException


class FakeDocker:
    """Answers ``docker ps`` and ``docker stats`` from canned outputs, one per call (an
    exception is raised instead), and records every call."""

    def __init__(self, ps: Sequence[Answer], stats: Sequence[Answer]) -> None:
        self.ps = list(ps)
        self.stats = list(stats)
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> str:
        self.calls.append(list(arguments))
        if arguments[0] == "ps":
            answer = self.ps.pop(0)
        else:
            assert arguments[0] == "stats", arguments
            answer = self.stats.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def project_ps() -> str:
    return "\n".join([ps_line(LOKI_ID, "loki"), ps_line(GRAFANA_ID, "grafana")]) + "\n"


def project_stats(loki: str, grafana: str) -> str:
    return "\n".join(
        [
            stats_line(LOKI_ID, "ccobs-mem-loki-1", f"{loki} / 512MiB"),
            stats_line(GRAFANA_ID, "ccobs-mem-grafana-1", f"{grafana} / 384MiB"),
            stats_line(OTHER_ID, "other-postgres-1", "45.05MiB / 512MiB"),
        ]
    )


def test_sample_reads_the_project_containers_then_their_stats() -> None:
    docker = FakeDocker([project_ps()], [project_stats("70MiB", "200MiB")])

    assert sample("ccobs-mem", docker) == {
        "loki": Usage(70 * MIB, 512 * MIB),
        "grafana": Usage(200 * MIB, 384 * MIB),
    }
    assert docker.calls == [ps_arguments("ccobs-mem"), stats_arguments()]


def test_sample_skips_docker_stats_when_the_project_has_no_container() -> None:
    docker = FakeDocker([""], [])

    assert sample("ccobs-mem", docker) == {}
    assert docker.calls == [ps_arguments("ccobs-mem")]


def test_sample_leaves_out_a_service_without_a_reading() -> None:
    stats = project_stats("70MiB", "200MiB").replace("70MiB / 512MiB", "-- / --")

    assert sample("ccobs-mem", FakeDocker([project_ps()], [stats])) == {
        "grafana": Usage(200 * MIB, 384 * MIB)
    }


def test_sample_is_not_upset_by_another_containers_line() -> None:
    stats = project_stats("70MiB", "200MiB").replace("45.05MiB / 512MiB", "-- / --")

    assert sample("ccobs-mem", FakeDocker([project_ps()], [stats])) == {
        "loki": Usage(70 * MIB, 512 * MIB),
        "grafana": Usage(200 * MIB, 384 * MIB),
    }


def run_main(
    capsys: pytest.CaptureFixture[str],
    docker: Callable[[Sequence[str]], str],
    *arguments: str,
) -> tuple[int, str, str, list[float]]:
    pauses: list[float] = []
    code = main(list(arguments), runner=docker, sleep=pauses.append)
    captured = capsys.readouterr()
    return code, captured.out, captured.err, pauses


def test_main_prints_a_table_of_one_sample(capsys: pytest.CaptureFixture[str]) -> None:
    docker = FakeDocker([project_ps()], [project_stats("70MiB", "192MiB")])

    code, out, err, pauses = run_main(capsys, docker)

    assert code == 0
    assert err == ""
    assert pauses == []
    lines = out.splitlines()
    assert lines[0] == "cc-observatory: 1 sample"
    assert lines[2].split() == ["grafana", "192.0MiB", "192.0MiB", "384.0MiB", "50.0%", "50.0%"]
    assert lines[3].split()[0] == "loki"
    assert lines[4].split()[:3] == ["total", "262.0MiB", "262.0MiB"]
    assert docker.calls[0] == ps_arguments("cc-observatory")


def test_main_samples_at_the_interval_and_keeps_the_peak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    docker = FakeDocker(
        [project_ps()] * 3,
        [
            project_stats("70MiB", "200MiB"),
            project_stats("90MiB", "300MiB"),
            project_stats("80MiB", "250MiB"),
        ],
    )

    code, out, _, pauses = run_main(
        capsys, docker, "--project", "ccobs-mem", "--samples", "3", "--interval", "2", "--json"
    )

    assert code == 0
    # A pause between two samples, none after the last.
    assert pauses == [2.0, 2.0]
    document = json.loads(out)
    assert document["project"] == "ccobs-mem"
    assert document["samples"] == 3
    services = {item["service"]: item for item in document["services"]}
    assert services["grafana"]["used_mib"] == 250.0
    assert services["grafana"]["peak_mib"] == 300.0
    assert services["loki"]["peak_mib"] == 90.0
    assert document["total"]["peak_mib"] == 390.0
    assert {tuple(call[:1]) for call in docker.calls} == {("ps",), ("stats",)}
    assert all("label=com.docker.compose.project=ccobs-mem" in call for call in docker.calls[::2])


def test_main_fails_when_the_project_has_no_running_container(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err, _ = run_main(capsys, FakeDocker(["", ""], []), "--samples", "2")

    assert code == 1
    assert out == ""
    assert "no running container" in err
    assert "cc-observatory" in err


def test_main_reports_a_docker_error(capsys: pytest.CaptureFixture[str]) -> None:
    def broken(arguments: Sequence[str]) -> str:
        raise MemoryToolError("docker is not on PATH")

    code, out, err, _ = run_main(capsys, broken)

    assert code == 1
    assert out == ""
    assert "docker is not on PATH" in err


def test_main_reports_output_it_cannot_read(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err, _ = run_main(capsys, FakeDocker(["not json"], []))

    assert code == 1
    assert out == ""
    assert "docker ps" in err


def test_main_skips_a_failed_sample_after_one_has_succeeded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A busy daemon can fail one reading at the very peak being measured; the samples
    # taken before it are not thrown away.
    docker = FakeDocker(
        [project_ps(), project_ps(), "not json", project_ps()],
        [
            project_stats("70MiB", "200MiB"),
            MemoryToolError("`docker stats` exited with 1: timeout"),
            project_stats("80MiB", "250MiB"),
        ],
    )

    code, out, err, pauses = run_main(capsys, docker, "--samples", "4", "--json")

    assert code == 0
    assert pauses == [5.0, 5.0, 5.0]
    assert err.splitlines() == [
        "memory: sample 2 of 4 skipped: `docker stats` exited with 1: timeout",
        f"memory: sample 3 of 4 skipped: docker ps line 1 is not a JSON object: {'not json'!r}",
    ]
    document = json.loads(out)
    assert document["samples"] == 2
    assert document["total"]["peak_mib"] == 330.0


def test_main_prints_the_summary_so_far_when_interrupted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    docker = FakeDocker(
        [project_ps(), project_ps(), KeyboardInterrupt()],
        [project_stats("70MiB", "200MiB"), project_stats("90MiB", "300MiB")],
    )

    code, out, err, _ = run_main(capsys, docker, "--samples", "60")

    assert code == 0
    assert err == "memory: interrupted after 2 samples\n"
    lines = out.splitlines()
    assert lines[0] == "cc-observatory: 2 samples, 5.0 s apart"
    assert lines[-1].split()[:3] == ["total", "390.0MiB", "390.0MiB"]


def test_main_interrupted_during_a_pause(capsys: pytest.CaptureFixture[str]) -> None:
    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    docker = FakeDocker([project_ps()], [project_stats("70MiB", "200MiB")])
    code = main(["--samples", "3"], runner=docker, sleep=interrupt)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == "memory: interrupted after 1 sample\n"
    assert captured.out.splitlines()[0] == "cc-observatory: 1 sample"


def test_main_interrupted_before_the_first_sample(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err, _ = run_main(capsys, FakeDocker([KeyboardInterrupt()], []), "--samples", "3")

    assert code == 1
    assert out == ""
    assert err == "memory: interrupted after 0 samples\n"


# The subprocess layer.


class FakeRun:
    """Stands in for ``subprocess.run``: records the call and returns a canned result
    or raises."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        error: Exception | None = None,
    ) -> None:
        self.result = subprocess.CompletedProcess[str]([], returncode, stdout, stderr)
        self.error = error
        self.command: list[str] = []
        self.options: dict[str, object] = {}

    def __call__(self, command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.command = command
        self.options = options
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def docker_on_path(monkeypatch: pytest.MonkeyPatch) -> str:
    path = "/usr/bin/docker"
    monkeypatch.setattr(shutil, "which", lambda name: path if name == "docker" else None)
    return path


def test_run_docker_returns_stdout(monkeypatch: pytest.MonkeyPatch, docker_on_path: str) -> None:
    fake = FakeRun(stdout="out\n")
    monkeypatch.setattr(subprocess, "run", fake)

    assert run_docker(["stats", "--no-stream"]) == "out\n"
    assert fake.command == [docker_on_path, "stats", "--no-stream"]
    assert fake.options["check"] is False
    assert fake.options["capture_output"] is True
    assert isinstance(fake.options["timeout"], float)


def test_run_docker_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(MemoryToolError, match="not on PATH"):
        run_docker(["ps"])


def test_run_docker_reports_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, docker_on_path: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", FakeRun(returncode=1, stderr="Cannot connect to the daemon\n")
    )

    with pytest.raises(MemoryToolError, match=r"docker stats.*1.*Cannot connect to the daemon"):
        run_docker(["stats", "--no-stream"])


@pytest.mark.parametrize(
    "error", [OSError("boom"), subprocess.TimeoutExpired(["docker"], 60.0)], ids=["os", "timeout"]
)
def test_run_docker_reports_a_failed_run(
    monkeypatch: pytest.MonkeyPatch, docker_on_path: str, error: Exception
) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(error=error))

    with pytest.raises(MemoryToolError, match="docker ps"):
        run_docker(["ps"])
