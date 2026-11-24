import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


# Run all test suites:
# python3 tests/run_all.py
#
# Specify the CLI executable:
# python3 tests/run_all.py ./build/ifc-cli
#
# Exclude performance benchmarks:
# python3 tests/run_all.py --skip-performance
#
# Execute selected suites:
# python3 tests/run_all.py --only domains integration
#
# Specify a report directory:
# python3 tests/run_all.py --output-dir ./reports
#
# Set a maximum execution time per suite:
# python3 tests/run_all.py --timeout 1800
#
# Display detailed suite output:
# python3 tests/run_all.py --verbose
#
# Reports:
# tests/reports/<timestamp>/report.md
# tests/reports/<timestamp>/report.json
# tests/reports/<timestamp>/performance.json
# tests/reports/<timestamp>/logs/<suite>.stdout.log
# tests/reports/<timestamp>/logs/<suite>.stderr.log
#
# No external Python dependency is required.
# Performance tests retain their own optional dependencies.
# Each suite runs in an isolated Python process.
# Suites run sequentially to avoid distorting performance measurements.
# Missing expected suites and unexpected process failures are reported.
# Reports are updated after each completed suite.
# Failed assertions do not interrupt the remaining suites.
#
# Unittest failures, errors and skips may include subtest events.
# Consequently, passed individual test counts are not inferred by
# subtracting failure events from the total number of executed tests.
#
# Not executed against the complete local test collection.

TEST_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent

DEFAULT_EXECUTABLE = PROJECT_DIRECTORY / "build" / "ifc-cli"
DEFAULT_REPORT_DIRECTORY = TEST_DIRECTORY / "reports"

EXPECTED_SUITES = (
    "test_protocol.py",
    "test_session.py",
    "test_identifiers.py",
    "test_transforms.py",
    "test_hierarchy.py",
    "test_placements.py",
    "test_properties.py",
    "test_geometries.py",
    "test_profiles.py",
    "test_boolean_operations.py",
    "test_materials.py",
    "test_material_assignments.py",
    "test_tessellation.py",
    "test_duplication.py",
    "test_deletion.py",
    "test_persistence.py",
    "test_units.py",
    "test_schemas.py",
    "test_native_entities.py",
    "test_domains.py",
    "test_domain_advanced.py",
    "test_robustness.py",
    "test_integration.py",
    "test_performance.py"
)

PERFORMANCE_SUITE = "test_performance.py"

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_ERROR = "ERROR"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_SKIPPED = "SKIPPED"
STATUS_MISSING = "MISSING"
STATUS_INTERRUPTED = "INTERRUPTED"

FAILURE_STATUSES = {
    STATUS_FAILED,
    STATUS_ERROR,
    STATUS_TIMEOUT,
    STATUS_MISSING,
    STATUS_INTERRUPTED
}

SUMMARY_FIELDS = (
    "failures",
    "errors",
    "skipped",
    "expectedFailures",
    "unexpectedSuccesses"
)

ANSI_PATTERN = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
)

UNITTEST_COUNT_PATTERN = re.compile(
    r"^Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s\s*$",
    re.MULTILINE
)

UNITTEST_STATUS_PATTERN = re.compile(
    r"^(OK|FAILED)(?:\s+\(([^)]*)\))?\s*$",
    re.MULTILINE
)

UNITTEST_DETAIL_PATTERN = re.compile(
    r"^(FAIL|ERROR|UNEXPECTED SUCCESS):\s*(.+)$",
    re.MULTILINE
)

UNITTEST_FIELD_PATTERN = re.compile(
    r"([a-zA-Z_ ]+)\s*=\s*(\d+)"
)

UNITTEST_FIELD_MAPPING = {
    "failures": "failures",
    "errors": "errors",
    "skipped": "skipped",
    "expected failures": "expectedFailures",
    "unexpected successes": "unexpectedSuccesses"
}


def utc_now():
    return datetime.now(timezone.utc)


def timestamp():
    return utc_now().strftime("%Y-%m-%d_%H-%M-%S_%f")


def format_duration(seconds):
    if seconds is None:
        return "-"

    if seconds < 60:
        return f"{seconds:.3f} s"

    minutes, seconds = divmod(seconds, 60)

    if minutes < 60:
        return f"{int(minutes)} min {seconds:.2f} s"

    hours, minutes = divmod(int(minutes), 60)

    return f"{hours} h {minutes} min {seconds:.2f} s"


def format_count(value):
    return "-" if value is None else str(value)


def markdown_escape(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def strip_ansi(content):
    return ANSI_PATTERN.sub("", content)


def read_text(path):
    if not path.is_file():
        return ""

    return path.read_text(
        encoding="utf-8",
        errors="replace"
    )


def write_json(path, data):
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    temporary_path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            allow_nan=False
        ) + "\n",
        encoding="utf-8"
    )

    temporary_path.replace(path)


def write_text(path, content):
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    temporary_path.write_text(
        content,
        encoding="utf-8"
    )

    temporary_path.replace(path)


def normalize_suite_name(name):
    name = Path(name).name

    if name.endswith(".py"):
        name = name[:-3]

    if not name.startswith("test_"):
        name = f"test_{name}"

    return f"{name}.py"


def discover_suites():
    discovered = {
        path.name
        for path in TEST_DIRECTORY.glob("test_*.py")
        if path.is_file()
    }

    expected = set(EXPECTED_SUITES)

    additional = sorted(
        discovered - expected
    )

    return [
        *EXPECTED_SUITES,
        *additional
    ]


def parse_unittest_output(content):
    content = strip_ansi(content)

    count_matches = list(
        UNITTEST_COUNT_PATTERN.finditer(content)
    )

    result = {
        "recognized": False,
        "status": None,
        "testsRun": None,
        "durationSeconds": None,
        "failures": None,
        "errors": None,
        "skipped": None,
        "expectedFailures": None,
        "unexpectedSuccesses": None,
        "details": []
    }

    if not count_matches:
        return result

    count_match = count_matches[-1]

    result["testsRun"] = int(count_match.group(1))

    result["durationSeconds"] = float(
        count_match.group(2)
    )

    summary_content = content[count_match.end():]

    status_matches = list(
        UNITTEST_STATUS_PATTERN.finditer(summary_content)
    )

    if not status_matches:
        return result

    status_match = status_matches[-1]

    result["recognized"] = True
    result["status"] = status_match.group(1)

    for field in SUMMARY_FIELDS:
        result[field] = 0

    fields = status_match.group(2) or ""

    for match in UNITTEST_FIELD_PATTERN.finditer(fields):
        name = match.group(1).strip()
        value = int(match.group(2))

        normalized_name = UNITTEST_FIELD_MAPPING.get(name)

        if normalized_name is not None:
            result[normalized_name] = value

    for match in UNITTEST_DETAIL_PATTERN.finditer(content):
        result["details"].append({
            "type": match.group(1),
            "test": match.group(2).strip()
        })

    return result


def parse_performance_metrics(content):
    metrics = []
    parsing_errors = []

    for line_number, line in enumerate(
        content.splitlines(),
        start=1
    ):
        line = strip_ansi(line).strip()

        if not line.startswith("PERF "):
            continue

        payload = line[len("PERF "):]

        try:
            metric = json.loads(
                payload,
                parse_constant=lambda value: (
                    reject_nonfinite_constant(value)
                )
            )

            if not isinstance(metric, dict):
                raise ValueError(
                    "Performance metric must be a JSON object"
                )

            if not isinstance(metric.get("test"), str):
                raise ValueError(
                    "Performance metric has no test name"
                )

            metrics.append(metric)

        except (json.JSONDecodeError, ValueError) as error:
            parsing_errors.append({
                "line": line_number,
                "message": str(error),
                "content": payload[:500]
            })

    return metrics, parsing_errors


def reject_nonfinite_constant(value):
    raise ValueError(
        f"Non-finite JSON constant: {value}"
    )


def create_suite_result(filename):
    return {
        "suite": Path(filename).stem,
        "filename": filename,
        "status": STATUS_PENDING,
        "startedAt": None,
        "finishedAt": None,
        "durationSeconds": None,
        "exitCode": None,
        "testsRun": None,
        "failures": None,
        "errors": None,
        "skipped": None,
        "expectedFailures": None,
        "unexpectedSuccesses": None,
        "unittestDurationSeconds": None,
        "unittestStatus": None,
        "message": None,
        "details": [],
        "stdoutLog": None,
        "stderrLog": None,
        "performanceMetricCount": 0,
        "performanceParsingErrors": []
    }


def terminate_process(process):
    # POSIX process groups also contain CLI subprocesses started
    # by the test suite, preventing orphaned CLI processes.

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    elif process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def execute_suite(
    result,
    executable,
    report_directory,
    timeout=None,
    verbose=False
):
    suite_path = TEST_DIRECTORY / result["filename"]

    if not suite_path.is_file():
        result["status"] = STATUS_MISSING
        result["message"] = f"Test suite not found: {suite_path}"

        return []

    logs_directory = report_directory / "logs"

    stdout_path = logs_directory / (
        f"{result['suite']}.stdout.log"
    )

    stderr_path = logs_directory / (
        f"{result['suite']}.stderr.log"
    )

    result["stdoutLog"] = stdout_path.relative_to(
        report_directory
    ).as_posix()

    result["stderrLog"] = stderr_path.relative_to(
        report_directory
    ).as_posix()

    command = [
        sys.executable,
        "-u",
        str(suite_path),
        str(executable)
    ]

    environment = os.environ.copy()

    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHON_COLORS"] = "0"
    environment["NO_COLOR"] = "1"

    result["status"] = STATUS_RUNNING
    result["startedAt"] = utc_now().isoformat()

    started = time.perf_counter()

    process = None

    try:
        with (
            stdout_path.open("wb") as stdout_file,
            stderr_path.open("wb") as stderr_file
        ):
            process = subprocess.Popen(
                command,
                cwd=PROJECT_DIRECTORY,
                stdout=stdout_file,
                stderr=stderr_file,
                env=environment,
                start_new_session=os.name == "posix"
            )

            try:
                result["exitCode"] = process.wait(
                    timeout=timeout
                )

            except subprocess.TimeoutExpired:
                terminate_process(process)

                result["exitCode"] = process.returncode
                result["status"] = STATUS_TIMEOUT

                result["message"] = (
                    f"Suite exceeded its {timeout:g} second timeout"
                )

            except KeyboardInterrupt:
                terminate_process(process)

                result["exitCode"] = process.returncode
                result["status"] = STATUS_INTERRUPTED
                result["message"] = "Interrupted by the user"

    except OSError as error:
        if process is not None and process.poll() is None:
            terminate_process(process)

        result["status"] = STATUS_ERROR
        result["message"] = str(error)

    finally:
        result["durationSeconds"] = (
            time.perf_counter() - started
        )

        result["finishedAt"] = utc_now().isoformat()

    stdout_content = read_text(stdout_path)
    stderr_content = read_text(stderr_path)

    # Unittest normally writes its report to stderr.
    # The stdout fallback supports suites using a custom output stream.

    unittest_result = parse_unittest_output(
        stderr_content
    )

    if not unittest_result["recognized"]:
        stdout_result = parse_unittest_output(
            stdout_content
        )

        if stdout_result["recognized"]:
            unittest_result = stdout_result

    result["testsRun"] = unittest_result["testsRun"]

    result["unittestDurationSeconds"] = (
        unittest_result["durationSeconds"]
    )

    result["unittestStatus"] = unittest_result["status"]

    result["details"] = unittest_result["details"]

    for field in SUMMARY_FIELDS:
        result[field] = unittest_result[field]

    metrics, parsing_errors = parse_performance_metrics(
        stdout_content
    )

    result["performanceMetricCount"] = len(metrics)

    result["performanceParsingErrors"] = parsing_errors

    if result["status"] == STATUS_RUNNING:
        if not unittest_result["recognized"]:
            result["status"] = STATUS_ERROR

            result["message"] = (
                "No complete unittest summary found in suite output"
            )

        elif result["testsRun"] == 0:
            result["status"] = STATUS_ERROR
            result["message"] = "No tests were executed"

        elif (
            unittest_result["status"] == "FAILED"
            or result["failures"] > 0
            or result["errors"] > 0
            or result["unexpectedSuccesses"] > 0
        ):
            result["status"] = STATUS_FAILED

        elif result["exitCode"] != 0:
            result["status"] = STATUS_ERROR

            result["message"] = (
                "Suite returned a non-zero exit code despite "
                "a successful unittest summary"
            )

        elif parsing_errors:
            result["status"] = STATUS_ERROR

            result["message"] = (
                "Invalid performance metric output"
            )

        else:
            result["status"] = STATUS_PASSED

    if verbose:
        print(f"\n--- {result['suite']} stdout ---")

        if stdout_content:
            print(stdout_content.rstrip())

        print(f"\n--- {result['suite']} stderr ---")

        if stderr_content:
            print(stderr_content.rstrip())

        print()

    return [
        {
            "suite": result["suite"],
            "metric": metric
        }
        for metric in metrics
    ]


def summarize_results(results):
    status_counts = Counter(
        result["status"]
        for result in results
    )

    executed_suites = [
        result
        for result in results
        if result["startedAt"] is not None
    ]

    recognized_suites = [
        result
        for result in results
        if result["unittestStatus"] is not None
    ]

    summary = {
        "suiteCount": len(results),
        "executedSuiteCount": len(executed_suites),
        "recognizedSuiteCount": len(recognized_suites),
        "passedSuiteCount": status_counts[STATUS_PASSED],
        "failedSuiteCount": status_counts[STATUS_FAILED],
        "errorSuiteCount": status_counts[STATUS_ERROR],
        "timeoutSuiteCount": status_counts[STATUS_TIMEOUT],
        "skippedSuiteCount": status_counts[STATUS_SKIPPED],
        "missingSuiteCount": status_counts[STATUS_MISSING],
        "interruptedSuiteCount": status_counts[STATUS_INTERRUPTED],
        "pendingSuiteCount": status_counts[STATUS_PENDING],
        "runningSuiteCount": status_counts[STATUS_RUNNING],
        "testsRun": sum(
            result["testsRun"] or 0
            for result in recognized_suites
        ),
        "suiteDurationSeconds": sum(
            result["durationSeconds"] or 0
            for result in executed_suites
        ),
        "performanceMetricCount": sum(
            result["performanceMetricCount"]
            for result in results
        ),
        "performanceParsingErrorCount": sum(
            len(result["performanceParsingErrors"])
            for result in results
        )
    }

    for field in SUMMARY_FIELDS:
        summary[field] = sum(
            result[field] or 0
            for result in recognized_suites
        )

    summary["successful"] = (
        summary["passedSuiteCount"] > 0
        and not any(
            result["status"] in FAILURE_STATUSES
            for result in results
        )
        and summary["pendingSuiteCount"] == 0
        and summary["runningSuiteCount"] == 0
    )

    summary["complete"] = (
        summary["pendingSuiteCount"] == 0
        and summary["runningSuiteCount"] == 0
        and summary["interruptedSuiteCount"] == 0
    )

    return summary


def format_environment(executable):
    return {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "logicalCpuCount": os.cpu_count(),
        "pythonVersion": platform.python_version(),
        "pythonExecutable": sys.executable,
        "cliExecutable": str(executable),
        "projectDirectory": str(PROJECT_DIRECTORY),
        "testDirectory": str(TEST_DIRECTORY),
        "executionMode": "sequential",
        "performanceRepetitions": os.environ.get(
            "IFC_CLI_PERF_REPETITIONS",
            "3"
        ),
        "performanceRequestTimeout": os.environ.get(
            "IFC_CLI_PERF_TIMEOUT",
            "300"
        )
    }


def build_report(
    results,
    performance_metrics,
    environment,
    started_at,
    duration,
    finished_at=None
):
    summary = summarize_results(results)

    if finished_at is None:
        status = "RUNNING"

    elif summary["interruptedSuiteCount"]:
        status = STATUS_INTERRUPTED

    elif summary["successful"]:
        status = STATUS_PASSED

    else:
        status = STATUS_FAILED

    return {
        "reportVersion": "1.0",
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "durationSeconds": duration,
        "environment": environment,
        "summary": summary,
        "suites": results,
        "performanceMetrics": performance_metrics,
        "notes": [
            (
                "Failure, error and skip counts may include "
                "unittest subtest events."
            ),
            (
                "Test totals include only suites for which a complete "
                "unittest summary was available."
            ),
            (
                "Performance benchmarks report measurements "
                "without imposing machine-dependent thresholds."
            ),
            (
                "Suite execution durations include Python process "
                "startup and shutdown."
            )
        ]
    }


def markdown_report(report):
    summary = report["summary"]
    environment = report["environment"]

    lines = [
        "# IFC CLI - Test Report",
        "",
        "## Overview",
        "",
        f"- Status: **{report['status']}**",
        f"- Started: `{report['startedAt']}`",
        f"- Finished: `{report['finishedAt'] or 'Running'}`",
        f"- Total duration: **{format_duration(report['durationSeconds'])}**",
        f"- Execution mode: {environment['executionMode']}",
        "",
        "## Environment",
        "",
        "| Parameter | Value |",
        "| --- | --- |",
        f"| Operating system | {markdown_escape(environment['platform'])} |",
        f"| Architecture | {markdown_escape(environment['architecture'])} |",
        f"| Processor | {markdown_escape(environment['processor'] or 'Unknown')} |",
        f"| Logical CPUs | {environment['logicalCpuCount']} |",
        f"| Python version | {environment['pythonVersion']} |",
        f"| Python executable | `{environment['pythonExecutable']}` |",
        f"| CLI executable | `{environment['cliExecutable']}` |",
        f"| Performance repetitions | {environment['performanceRepetitions']} |",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| Total suites | {summary['suiteCount']} |",
        f"| Executed suites | {summary['executedSuiteCount']} |",
        f"| Successful suites | {summary['passedSuiteCount']} |",
        f"| Failed suites | {summary['failedSuiteCount']} |",
        f"| Suite execution errors | {summary['errorSuiteCount']} |",
        f"| Timed-out suites | {summary['timeoutSuiteCount']} |",
        f"| Skipped suites | {summary['skippedSuiteCount']} |",
        f"| Missing suites | {summary['missingSuiteCount']} |",
        f"| Interrupted suites | {summary['interruptedSuiteCount']} |",
        f"| Pending suites | {summary['pendingSuiteCount']} |",
        f"| Executed tests | {summary['testsRun']} |",
        f"| Failure events | {summary['failures']} |",
        f"| Error events | {summary['errors']} |",
        f"| Skip events | {summary['skipped']} |",
        f"| Expected failures | {summary['expectedFailures']} |",
        f"| Unexpected successes | {summary['unexpectedSuccesses']} |",
        f"| Performance metrics | {summary['performanceMetricCount']} |",
        "",
        "## Suite Results",
        "",
        "| Suite | Status | Tests | Failures | Errors | Skipped | Duration | Logs |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"
    ]

    for result in report["suites"]:
        links = []

        if result["stdoutLog"] is not None:
            links.append(
                f"[stdout]({result['stdoutLog']})"
            )

        if result["stderrLog"] is not None:
            links.append(
                f"[stderr]({result['stderrLog']})"
            )

        log_links = " / ".join(links) if links else "-"

        lines.append(
            "| "
            f"`{result['filename']}` | "
            f"**{result['status']}** | "
            f"{format_count(result['testsRun'])} | "
            f"{format_count(result['failures'])} | "
            f"{format_count(result['errors'])} | "
            f"{format_count(result['skipped'])} | "
            f"{format_duration(result['durationSeconds'])} | "
            f"{log_links} |"
        )

    lines.extend([
        "",
        "## Failed Suites",
        ""
    ])

    failed_results = [
        result
        for result in report["suites"]
        if result["status"] in FAILURE_STATUSES
    ]

    if not failed_results:
        lines.append("No failed suites recorded.")

    for result in failed_results:
        lines.extend([
            f"### {result['filename']}",
            "",
            f"- Status: **{result['status']}**",
            f"- Exit code: `{result['exitCode']}`",
            f"- Duration: {format_duration(result['durationSeconds'])}"
        ])

        if result["message"]:
            lines.append(
                f"- Message: {markdown_escape(result['message'])}"
            )

        if result["details"]:
            lines.extend([
                "",
                "| Type | Test |",
                "| --- | --- |"
            ])

            for detail in result["details"]:
                lines.append(
                    f"| {detail['type']} | "
                    f"{markdown_escape(detail['test'])} |"
                )

        if result["performanceParsingErrors"]:
            lines.extend([
                "",
                "Performance metric parsing errors:",
                ""
            ])

            for error in result["performanceParsingErrors"]:
                lines.append(
                    f"- Line {error['line']}: "
                    f"{markdown_escape(error['message'])}"
                )

        lines.append("")

        if result["stderrLog"]:
            lines.append(
                f"[Complete error log]({result['stderrLog']})"
            )

        if result["stdoutLog"]:
            lines.append(
                f"[Standard output]({result['stdoutLog']})"
            )

        lines.append("")

    lines.extend([
        "",
        "## Performance Measurements",
        ""
    ])

    performance_metrics = report["performanceMetrics"]

    if not performance_metrics:
        lines.append("No performance measurements recorded.")

    else:
        metric_counts = Counter(
            item["metric"]["test"]
            for item in performance_metrics
        )

        lines.extend([
            "| Benchmark | Metric records |",
            "| --- | ---: |"
        ])

        for name, count in sorted(metric_counts.items()):
            lines.append(
                f"| {markdown_escape(name)} | {count} |"
            )

        lines.extend([
            "",
            "[Complete performance metrics](performance.json)",
            "",
            "No machine-dependent performance thresholds are applied."
        ])

        lines.extend(
            performance_timing_table(performance_metrics)
        )

    lines.extend([
        "",
        "## Execution Notes",
        ""
    ])

    for note in report["notes"]:
        lines.append(f"- {note}")

    lines.extend([
        "",
        "## Report Files",
        "",
        "- [Machine-readable report](report.json)",
        "- [Performance measurements](performance.json)",
        "",
        "---",
        "",
        "Generated by `tests/run_all.py`.",
        ""
    ])

    return "\n".join(lines)


def performance_timing_table(performance_metrics):
    rows = []

    for entry in performance_metrics:
        metric = entry["metric"]

        duration = metric.get("durationSeconds")

        if not isinstance(duration, dict):
            continue

        median = duration.get("median")

        if not isinstance(median, (int, float)):
            continue

        parameters = []

        for field in (
            "productCount",
            "entityCount",
            "returnedEntityCount",
            "indexedEntityCount",
            "profileVertexCount",
            "domainCount",
            "operationCount",
            "descendantCount",
            "entityType",
            "modelType",
            "command",
            "kind",
            "includeNormals"
        ):
            if field in metric:
                parameters.append(
                    f"{field}={metric[field]}"
                )

        throughput = metric.get("operationsPerSecond")

        if isinstance(throughput, (int, float)):
            throughput_text = f"{throughput:.2f} operations/s"

        else:
            streaming = metric.get(
                "streamingMiBPerSecond"
            )

            if isinstance(streaming, dict):
                streaming = streaming.get("median")

            if isinstance(streaming, (int, float)):
                throughput_text = f"{streaming:.2f} MiB/s"
            else:
                throughput_text = "-"

        rows.append(
            "| "
            f"{markdown_escape(metric['test'])} | "
            f"{markdown_escape(', '.join(parameters) or '-')} | "
            f"{format_duration(duration.get('minimum'))} | "
            f"{format_duration(median)} | "
            f"{format_duration(duration.get('maximum'))} | "
            f"{throughput_text} |"
        )

    if not rows:
        return []

    return [
        "",
        "### Timing Results",
        "",
        "| Benchmark | Parameters | Minimum | Median | Maximum | Throughput |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
        *rows,
        ""
    ]


def save_reports(report_directory, report):
    write_json(
        report_directory / "report.json",
        report
    )

    write_json(
        report_directory / "performance.json",
        {
            "environment": report["environment"],
            "metricCount": len(report["performanceMetrics"]),
            "metrics": report["performanceMetrics"]
        }
    )

    write_text(
        report_directory / "report.md",
        markdown_report(report)
    )


def print_suite_result(result):
    status = result["status"]

    duration = format_duration(
        result["durationSeconds"]
    )

    tests = format_count(
        result["testsRun"]
    )

    failures = format_count(
        result["failures"]
    )

    errors = format_count(
        result["errors"]
    )

    skipped = format_count(
        result["skipped"]
    )

    print(
        f"  {status}: "
        f"{tests} tests, "
        f"{failures} failures, "
        f"{errors} errors, "
        f"{skipped} skipped "
        f"({duration})",
        flush=True
    )

    if result["message"]:
        print(
            f"  {result['message']}",
            flush=True
        )

    for detail in result["details"]:
        print(
            f"  {detail['type']}: {detail['test']}",
            flush=True
        )

    if status in FAILURE_STATUSES and result["stderrLog"]:
        print(
            f"  Error log: {result['stderrLog']}",
            flush=True
        )


def print_final_summary(report, report_directory):
    summary = report["summary"]

    print()
    print("=" * 72)
    print("IFC CLI TEST SUMMARY")
    print("=" * 72)

    print(f"Overall status:          {report['status']}")
    print(f"Total suites:            {summary['suiteCount']}")
    print(f"Executed suites:         {summary['executedSuiteCount']}")
    print(f"Successful suites:       {summary['passedSuiteCount']}")
    print(f"Failed suites:           {summary['failedSuiteCount']}")
    print(f"Suite execution errors:  {summary['errorSuiteCount']}")
    print(f"Timed-out suites:        {summary['timeoutSuiteCount']}")
    print(f"Skipped suites:          {summary['skippedSuiteCount']}")
    print(f"Missing suites:          {summary['missingSuiteCount']}")
    print(f"Interrupted suites:      {summary['interruptedSuiteCount']}")
    print(f"Pending suites:          {summary['pendingSuiteCount']}")

    print("-" * 72)

    print(f"Executed tests:          {summary['testsRun']}")
    print(f"Failure events:          {summary['failures']}")
    print(f"Error events:            {summary['errors']}")
    print(f"Skip events:             {summary['skipped']}")
    print(f"Expected failures:       {summary['expectedFailures']}")
    print(f"Unexpected successes:    {summary['unexpectedSuccesses']}")
    print(f"Performance metrics:     {summary['performanceMetricCount']}")

    print("-" * 72)

    print(
        "Total duration:          "
        f"{format_duration(report['durationSeconds'])}"
    )

    failed = [
        result
        for result in report["suites"]
        if result["status"] in FAILURE_STATUSES
    ]

    if failed:
        print()
        print("Failed suites:")

        for result in failed:
            print(
                f"  {result['filename']} [{result['status']}]"
            )

    print()
    print("Reports:")
    print(f"  Markdown:    {report_directory / 'report.md'}")
    print(f"  JSON:        {report_directory / 'report.json'}")
    print(f"  Performance: {report_directory / 'performance.json'}")
    print(f"  Logs:        {report_directory / 'logs'}")

    print("=" * 72)


def positive_float(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Expected a positive number"
        ) from error

    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError(
            "Expected a finite positive number"
        )

    return number


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Execute IFC CLI integration test suites and "
            "generate structured test reports."
        )
    )

    parser.add_argument(
        "executable",
        nargs="?",
        type=Path,
        default=DEFAULT_EXECUTABLE,
        help=(
            "Path to the IFC CLI executable "
            f"(default: {DEFAULT_EXECUTABLE})"
        )
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_REPORT_DIRECTORY,
        help=(
            "Parent directory for timestamped reports "
            f"(default: {DEFAULT_REPORT_DIRECTORY})"
        )
    )

    parser.add_argument(
        "--skip-performance",
        action="store_true",
        help="Exclude the performance benchmark suite"
    )

    parser.add_argument(
        "--only",
        nargs="+",
        metavar="SUITE",
        help=(
            "Execute only selected suites, for example "
            "--only domains integration"
        )
    )

    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=None,
        metavar="SECONDS",
        help=(
            "Maximum execution time per suite in seconds "
            "(default: no suite-level timeout)"
        )
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Display complete suite output after execution"
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered and expected test suites without running them"
    )

    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first unsuccessful suite"
    )

    return parser.parse_args()


def select_suites(arguments):
    suites = discover_suites()

    if arguments.only:
        requested = {
            normalize_suite_name(name)
            for name in arguments.only
        }

        unknown = requested - set(suites)

        if unknown:
            raise ValueError(
                "Unknown test suites: "
                + ", ".join(sorted(unknown))
            )

        suites = [
            filename
            for filename in suites
            if filename in requested
        ]

    return suites


def main():
    arguments = parse_arguments()

    try:
        suites = select_suites(arguments)

    except ValueError as error:
        print(
            f"Error: {error}",
            file=sys.stderr
        )

        return 2

    if arguments.list:
        print("Available IFC CLI test suites:")

        for index, filename in enumerate(suites, start=1):
            path = TEST_DIRECTORY / filename

            if not path.is_file():
                status = STATUS_MISSING

            elif (
                arguments.skip_performance
                and filename == PERFORMANCE_SUITE
            ):
                status = STATUS_SKIPPED

            else:
                status = "AVAILABLE"

            print(
                f"  {index:02d}. {filename} [{status}]"
            )

        print(f"\nTotal: {len(suites)} suites")

        return 0

    executable = arguments.executable.expanduser().resolve()

    if not executable.is_file():
        print(
            f"Error: CLI executable not found: {executable}",
            file=sys.stderr
        )

        return 2

    if not os.access(executable, os.X_OK):
        print(
            f"Error: CLI executable is not executable: {executable}",
            file=sys.stderr
        )

        return 2

    report_directory = (
        arguments.output_dir.expanduser().resolve()
        / timestamp()
    )

    try:
        report_directory.mkdir(
            parents=True,
            exist_ok=False
        )

        (report_directory / "logs").mkdir()

    except OSError as error:
        print(
            f"Error: could not create report directory: {error}",
            file=sys.stderr
        )

        return 2

    results = [
        create_suite_result(filename)
        for filename in suites
    ]

    for result in results:
        if (
            arguments.skip_performance
            and result["filename"] == PERFORMANCE_SUITE
        ):
            result["status"] = STATUS_SKIPPED
            result["message"] = "Excluded by --skip-performance"

    performance_metrics = []

    environment = format_environment(
        executable
    )

    started_at = utc_now().isoformat()
    started = time.perf_counter()

    interrupted = False

    print("=" * 72)
    print("IFC CLI TEST RUNNER")
    print("=" * 72)

    print(f"Executable: {executable}")
    print(f"Test directory: {TEST_DIRECTORY}")
    print(f"Report directory: {report_directory}")
    print(f"Suites: {len(results)}")
    print(f"Python: {platform.python_version()}")
    print("Execution mode: sequential")

    if arguments.timeout is not None:
        print(
            f"Suite timeout: {arguments.timeout:g} seconds"
        )

    print("=" * 72)

    def persist(finished_at=None):
        report = build_report(
            results=results,
            performance_metrics=performance_metrics,
            environment=environment,
            started_at=started_at,
            duration=time.perf_counter() - started,
            finished_at=finished_at
        )

        save_reports(
            report_directory,
            report
        )

        return report

    persist()

    try:
        for index, result in enumerate(results, start=1):
            print(
                f"\n[{index:02d}/{len(results):02d}] "
                f"{result['filename']}",
                flush=True
            )

            if result["status"] == STATUS_SKIPPED:
                print(
                    f"  SKIPPED: {result['message']}",
                    flush=True
                )

                persist()
                continue

            metrics = execute_suite(
                result=result,
                executable=executable,
                report_directory=report_directory,
                timeout=arguments.timeout,
                verbose=arguments.verbose
            )

            performance_metrics.extend(metrics)

            print_suite_result(result)

            persist()

            if result["status"] == STATUS_INTERRUPTED:
                interrupted = True
                break

            if (
                arguments.fail_fast
                and result["status"] in FAILURE_STATUSES
            ):
                print(
                    "\nStopping after the first unsuccessful suite.",
                    flush=True
                )

                break

    except KeyboardInterrupt:
        interrupted = True

        for result in results:
            if result["status"] == STATUS_RUNNING:
                result["status"] = STATUS_INTERRUPTED
                result["message"] = "Interrupted by the user"

        print(
            "\nExecution interrupted.",
            flush=True
        )

    finally:
        for result in results:
            if result["status"] == STATUS_PENDING:
                result["status"] = STATUS_SKIPPED

                result["message"] = (
                    "Not executed because the run was interrupted"
                    if interrupted
                    else "Not executed because fail-fast stopped the run"
                )

        report = persist(
            finished_at=utc_now().isoformat()
        )

        if interrupted:
            report["status"] = STATUS_INTERRUPTED
            report["summary"]["successful"] = False
            report["summary"]["complete"] = False

            save_reports(
                report_directory,
                report
            )

        print_final_summary(
            report,
            report_directory
        )

    if interrupted:
        return 130

    if not report["summary"]["successful"]:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())