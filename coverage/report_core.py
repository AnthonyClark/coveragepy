# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Reporter foundation for coverage.py."""

# pragma: enforce 100% coverage

from __future__ import annotations

import concurrent.futures
import contextlib
import os
import sys
from collections.abc import Callable, Generator, Iterable
from typing import IO, TYPE_CHECKING, Protocol

from coverage.exceptions import ConfigError, NoDataError, NotPython
from coverage.files import GlobMatcher, prep_patterns
from coverage.misc import ensure_dir_for_file, file_be_gone
from coverage.parser import PythonParser
from coverage.plugin import FileReporter
from coverage.python import PythonFileReporter
from coverage.results import Analysis, analysis_from_measurements
from coverage.types import TArc, TLineNo, TMorf, TMorfs

if TYPE_CHECKING:
    from coverage import Coverage
    from coverage.config import CoverageConfig

TWorkerTask = tuple[str, str, list[TLineNo] | None, list[TArc] | None]
TWorkerResult = tuple[Analysis, PythonParser] | None


class Reporter(Protocol):
    """What we expect of reporters."""

    report_type: str

    def report(self, morfs: TMorfs, outfile: IO[str]) -> float:
        """Generate a report of `morfs`, written to `outfile`."""
        ...


def render_report(
    output_path: str,
    reporter: Reporter,
    morfs: TMorfs,
    msgfn: Callable[[str], None],
) -> float:
    """Run a one-file report generator, managing the output file.

    This function ensures the output file is ready to be written to. Then writes
    the report to it. Then closes the file and cleans up.

    """
    file_to_close = None
    delete_file = False

    if output_path == "-":
        outfile = sys.stdout
    else:
        # Ensure that the output directory is created; done here because this
        # report pre-opens the output file.  HtmlReporter does this on its own
        # because its task is more complex, being multiple files.
        ensure_dir_for_file(output_path)
        outfile = open(output_path, "w", encoding="utf-8")
        file_to_close = outfile
        delete_file = True

    try:
        ret = reporter.report(morfs, outfile=outfile)
        if file_to_close is not None:
            msgfn(f"Wrote {reporter.report_type} to {output_path}")
        delete_file = False
        return ret
    finally:
        if file_to_close is not None:
            file_to_close.close()
            if delete_file:
                file_be_gone(output_path)  # pragma: part covered (doesn't return)


def get_analysis_to_report(
    coverage: Coverage,
    morfs: TMorfs,
) -> Iterable[tuple[FileReporter, Analysis]]:
    """Get the files to report on.

    For each morf in `morfs`, if it should be reported on (based on the omit
    and include configuration options), yield a pair, the `FileReporter` and
    `Analysis` for the morf.

    """
    fr_morfs = coverage._get_file_reporters(morfs)
    config = coverage.config

    if config.report_include:
        matcher = GlobMatcher(prep_patterns(config.report_include), "report_include")
        fr_morfs = [(fr, morf) for (fr, morf) in fr_morfs if matcher.match(fr.filename)]

    if config.report_omit:
        matcher = GlobMatcher(prep_patterns(config.report_omit), "report_omit")
        fr_morfs = [(fr, morf) for (fr, morf) in fr_morfs if not matcher.match(fr.filename)]

    if not fr_morfs:
        raise NoDataError("No data to report.")

    # fr_morfs is a complete list of all files. FileReporters start very small,
    # but when you use one, it becomes large. We don't want to hold onto them
    # longer than we need to, so instead of a for loop, we'll pop and discard
    # the file reporters as we go.
    fr_morfs.sort(reverse=True)
    jobs = min(report_jobs(config.jobs), len(fr_morfs))
    prepared = _analyze_in_processes(coverage, (fr for fr, _ in reversed(fr_morfs)), jobs)
    with contextlib.closing(prepared):
        while fr_morfs:
            fr, morf = fr_morfs.pop()
            result = next(prepared, None)
            if result is None:
                analysis = _analyze_one(coverage, fr, morf)
            else:
                analysis, parser = result
                assert isinstance(fr, PythonFileReporter)
                fr._parser = parser
                fr._source = parser.text
            if analysis is not None:
                yield (fr, analysis)


def _analyze_one(coverage: Coverage, fr: FileReporter, morf: TMorf) -> Analysis | None:
    """Analyze one file in this process, warning about errors if configured to."""
    config = coverage.config
    try:
        return coverage._analyze(morf, file_reporter=fr)
    except NotPython:
        # Only report errors for .py files, and only if we didn't
        # explicitly suppress those errors.
        # NotPython is only raised by PythonFileReporter, which has a
        # should_be_python() method.
        if fr.should_be_python():  # type: ignore[attr-defined]
            if config.ignore_errors:
                msg = f"Couldn't parse Python file '{fr.filename}'"
                coverage._warn(msg, slug="couldnt-parse")
            else:
                raise
    except Exception as exc:
        if config.ignore_errors:
            msg = f"Couldn't parse '{fr.filename}': {exc}".rstrip()
            coverage._warn(msg, slug="couldnt-parse")
        else:
            raise
    return None


def report_jobs(jobs: int | str) -> int:
    """Interpret the `[report] jobs` setting as a number of processes."""
    if isinstance(jobs, str) and jobs.strip().lower() == "auto":
        return os.cpu_count() or 1
    try:
        njobs = int(jobs)
    except ValueError:
        njobs = -1
    if njobs < 0:
        raise ConfigError(f"[report] jobs must be 'auto' or a non-negative integer, not {jobs!r}")
    return max(njobs, 1)


_worker_coverage: Coverage | None = None


def _init_worker(config: CoverageConfig) -> None:
    """Set up a worker process to analyze files with `config`."""
    from coverage.control import Coverage
    from coverage.files import set_relative_directory

    global _worker_coverage
    _worker_coverage = Coverage(config_file=False)
    _worker_coverage.config = config
    set_relative_directory()


def _analyze_in_worker(
    tasks: list[TWorkerTask],
    has_arcs: bool,
    precision: int,
) -> list[TWorkerResult]:
    """Analyze Python files in a worker process."""
    results: list[TWorkerResult] = []
    for filename, mapped_filename, lines, arcs in tasks:
        try:
            fr = PythonFileReporter(filename, _worker_coverage)
            analysis = analysis_from_measurements(
                precision, fr, mapped_filename, has_arcs, lines, arcs
            )
        except Exception:
            results.append(None)
        else:
            results.append((analysis, fr.parser))
    return results


def _analyze_in_processes(
    coverage: Coverage,
    frs: Iterable[FileReporter],
    jobs: int,
) -> Generator[TWorkerResult]:
    """Analyze Python files in worker processes.

    Yields one item for each of `frs`, in order: the Analysis and the parsed
    source, or None if the file must be analyzed in this process instead.
    Yields nothing if `jobs` is less than 2.

    The data file is only read here: workers get the measured lines and arcs
    for each file, so they work for any data, including remapped paths and
    in-memory data.

    """
    filenames = []
    python_files = []
    for i, fr in enumerate(frs):
        filenames.append(fr.filename)
        if fr.__class__ is PythonFileReporter:
            python_files.append(i)
    return _analyze_python_files(coverage, filenames, python_files, jobs)


def _analyze_python_files(
    coverage: Coverage,
    filenames: list[str],
    python_files: list[int],
    jobs: int,
) -> Generator[TWorkerResult]:
    """The generator for `_analyze_in_processes`."""
    if jobs < 2 or not python_files:
        return
    data = coverage.get_data()
    has_arcs = data.has_arcs()
    precision = coverage.config.precision
    chunk_size = max(1, min(100, -(-len(python_files) // (jobs * 4))))
    to_submit = [
        python_files[start : start + chunk_size]
        for start in range(0, len(python_files), chunk_size)
    ]
    to_submit.reverse()
    submitted: list[tuple[list[int], concurrent.futures.Future[list[TWorkerResult]]]] = []
    failed = False

    def warn_failed(exc: Exception) -> None:
        nonlocal failed
        if not failed:
            coverage._warn(f"Couldn't analyze files in parallel: {exc}", slug="no-parallel")
            failed = True
        to_submit.clear()

    def submit(executor: concurrent.futures.Executor) -> None:
        while to_submit and len(submitted) < jobs * 2:
            indexes = to_submit.pop()
            tasks: list[TWorkerTask] = []
            for i in indexes:
                filename = filenames[i]
                mapped = coverage._file_mapper(filename)
                arcs = data.arcs(mapped) if has_arcs else None
                tasks.append((filename, mapped, data.lines(mapped), arcs))
            try:
                future = executor.submit(_analyze_in_worker, tasks, has_arcs, precision)
            except Exception as exc:
                warn_failed(exc)
                return
            submitted.append((indexes, future))

    with contextlib.ExitStack() as stack:
        stack.enter_context(data.keep_db_open())
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=jobs,
            initializer=_init_worker,
            initargs=(coverage.config,),
        )
        stack.callback(executor.shutdown, cancel_futures=True)
        submit(executor)

        results: dict[int, TWorkerResult] = {}
        for i in range(len(filenames)):  # pragma: part covered
            if submitted and submitted[0][0][0] == i:
                indexes, future = submitted.pop(0)
                try:
                    results.update(zip(indexes, future.result()))
                except Exception as exc:
                    warn_failed(exc)
                submit(executor)
            yield results.pop(i, None)
