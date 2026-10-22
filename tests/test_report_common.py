# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Tests of behavior common to all reporting."""

from __future__ import annotations

import os
import re
import textwrap
from unittest import mock

import pytest

import coverage
from coverage.exceptions import ConfigError, NotPython
from coverage.files import abs_file
from coverage.report_core import report_jobs
from tests.coveragetest import CoverageTest
from tests.goldtest import contains, doesnt_contain
from tests.helpers import arcz_to_arcs, os_sep


class ReportMapsPathsTest(CoverageTest):
    """Check that reporting implicitly maps paths."""

    def make_files(self, data: str, settings: bool = False) -> None:
        """Create the test files we need for line coverage."""
        src = """\
            if VER == 1:
                print("line 2")
            if VER == 2:
                print("line 4")
            if VER == 3:
                print("line 6")
            """
        self.make_file("src/program.py", src)
        self.make_file("ver1/program.py", src)
        self.make_file("ver2/program.py", src)

        if data == "line":
            self.make_data_file(
                lines={
                    abs_file("ver1/program.py"): [1, 2, 3, 5],
                    abs_file("ver2/program.py"): [1, 3, 4, 5],
                },
            )
        else:
            self.make_data_file(
                arcs={
                    abs_file("ver1/program.py"): arcz_to_arcs(".1 12 23 35 5."),
                    abs_file("ver2/program.py"): arcz_to_arcs(".1 13 34 45 5."),
                },
            )

        if settings:
            self.make_file(
                ".coveragerc",
                """\
                [paths]
                source =
                    src
                    ver1
                    ver2
                """,
            )

    def test_map_paths_during_line_report_without_setting(self) -> None:
        self.make_files(data="line")
        cov = coverage.Coverage()
        cov.load()
        cov.report(show_missing=True)
        expected = textwrap.dedent(
            os_sep("""\
            Name              Stmts   Miss  Cover   Missing
            -----------------------------------------------
            ver1/program.py       6      2    67%   4, 6
            ver2/program.py       6      2    67%   2, 6
            -----------------------------------------------
            TOTAL                12      4    67%
            """)
        )
        assert expected == self.stdout()

    def test_map_paths_during_line_report(self) -> None:
        self.make_files(data="line", settings=True)
        cov = coverage.Coverage()
        cov.load()
        cov.report(show_missing=True)
        expected = textwrap.dedent(
            os_sep("""\
            Name             Stmts   Miss  Cover   Missing
            ----------------------------------------------
            src/program.py       6      1    83%   6
            ----------------------------------------------
            TOTAL                6      1    83%
            """)
        )
        assert expected == self.stdout()

    def test_map_paths_during_branch_report_without_setting(self) -> None:
        self.make_files(data="arcs")
        cov = coverage.Coverage(branch=True)
        cov.load()
        cov.report(show_missing=True)
        expected = textwrap.dedent(
            os_sep("""\
            Name              Stmts   Miss Branch BrPart  Cover   Missing
            -------------------------------------------------------------
            ver1/program.py       6      2      6      3    58%   1->3, 4, 6
            ver2/program.py       6      2      6      3    58%   2, 3->5, 6
            -------------------------------------------------------------
            TOTAL                12      4     12      6    58%
            """)
        )
        assert expected == self.stdout()

    def test_map_paths_during_branch_report(self) -> None:
        self.make_files(data="arcs", settings=True)
        cov = coverage.Coverage(branch=True)
        cov.load()
        cov.report(show_missing=True)
        expected = textwrap.dedent(
            os_sep("""\
            Name             Stmts   Miss Branch BrPart  Cover   Missing
            ------------------------------------------------------------
            src/program.py       6      1      6      1    83%   6
            ------------------------------------------------------------
            TOTAL                6      1      6      1    83%
            """)
        )
        assert expected == self.stdout()

    def test_map_paths_during_annotate(self) -> None:
        self.make_files(data="line", settings=True)
        cov = coverage.Coverage()
        cov.load()
        cov.annotate()
        self.assert_exists(os_sep("src/program.py,cover"))
        self.assert_doesnt_exist(os_sep("ver1/program.py,cover"))
        self.assert_doesnt_exist(os_sep("ver2/program.py,cover"))

    def test_map_paths_during_html_report(self) -> None:
        self.make_files(data="line", settings=True)
        cov = coverage.Coverage()
        cov.load()
        cov.html_report()
        contains("htmlcov/index.html", f'src<span class="sep">{os_sep("/")}</span>program.py')
        doesnt_contain("htmlcov/index.html", "ver1", "ver2")

    def test_map_paths_during_xml_report(self) -> None:
        self.make_files(data="line", settings=True)
        cov = coverage.Coverage()
        cov.load()
        cov.xml_report()
        contains("coverage.xml", "src/program.py")
        doesnt_contain("coverage.xml", "ver1", "ver2")

    def test_map_paths_during_json_report(self) -> None:
        self.make_files(data="line", settings=True)
        cov = coverage.Coverage()
        cov.load()
        cov.json_report()

        def os_sepj(s: str) -> str:
            return os_sep(s).replace("\\", r"\\")

        contains("coverage.json", os_sepj("src/program.py"))
        doesnt_contain("coverage.json", "ver1", "ver2")

    def test_map_paths_during_lcov_report(self) -> None:
        self.make_files(data="line", settings=True)
        cov = coverage.Coverage()
        cov.load()
        cov.lcov_report()
        contains("coverage.lcov", os_sep("src/program.py"))
        doesnt_contain("coverage.lcov", "ver1", "ver2")


class ReportWithJinjaTest(CoverageTest):
    """Tests of Jinja-like behavior.

    Jinja2 compiles a template into Python code, and then runs the Python code
    to render the template.  But during rendering, it uses the template name
    (for example, "template.j2") as the file name, not the Python code file
    name.  Then during reporting, we will try to parse template.j2 as Python
    code.

    If the file can be parsed, it's included in the report (as a Python file!).
    If it can't be parsed, then it's not included in the report.

    These tests confirm that code doesn't raise an exception (as reported in
    #1553), and that the current (incorrect) behavior remains stable.  Ideally,
    good.j2 wouldn't be listed at all, since we can't report on it accurately.

    See https://github.com/coveragepy/coveragepy/issues/1553 for more detail, and
    https://github.com/coveragepy/coveragepy/issues/1623 for an issue about this
    behavior.

    """

    def make_files(self) -> None:
        """Create test files: two Jinja templates, and data from rendering them."""
        # A Jinja2 file that is syntactically acceptable Python (though it wont run).
        self.make_file(
            "good.j2",
            """\
            {{ data }}
            line2
            line3
            """,
        )
        # A Jinja2 file that is a Python syntax error.
        self.make_file(
            "bad.j2",
            """\
            This is data: {{ data }}.
            line 2
            line 3
            """,
        )
        self.make_data_file(
            lines={
                abs_file("good.j2"): [1, 3, 5, 7, 9],
                abs_file("bad.j2"): [1, 3, 5, 7, 9],
            },
        )

    def test_report(self) -> None:
        self.make_files()
        cov = coverage.Coverage()
        cov.load()
        cov.report(show_missing=True)
        expected = textwrap.dedent("""\
            Name      Stmts   Miss  Cover   Missing
            ---------------------------------------
            good.j2       3      1    67%   2
            ---------------------------------------
            TOTAL         3      1    67%
            """)
        assert expected == self.stdout()

    def test_html(self) -> None:
        self.make_files()
        cov = coverage.Coverage()
        cov.load()
        cov.html_report()
        contains(
            "htmlcov/index.html",
            """\
        <tbody>
            <tr class="region">
                <td class="name"><a href="good_j2.html">good.j2</a></td>
                <td class="spacer">&nbsp;</td>
                <td>3</td>
                <td>1</td>
                <td>0</td>
                <td class="spacer">&nbsp;</td>
                <td data-ratio="2 3">67%</td>
            </tr>
        </tbody>""",
        )
        doesnt_contain("htmlcov/index.html", "bad.j2")

    def test_xml(self) -> None:
        self.make_files()
        cov = coverage.Coverage()
        cov.load()
        cov.xml_report()
        contains("coverage.xml", 'filename="good.j2"')
        contains(
            "coverage.xml",
            '<line number="1" hits="1"/>',
            '<line number="2" hits="0"/>',
            '<line number="3" hits="1"/>',
        )
        doesnt_contain("coverage.xml", 'filename="bad.j2"')
        doesnt_contain("coverage.xml", '<line number="4"')

    def test_json(self) -> None:
        self.make_files()
        cov = coverage.Coverage()
        cov.load()
        cov.json_report()
        contains(
            "coverage.json",
            '"files": {"good.j2": {"executed_lines": [1, 3], '
            + '"summary": {"covered_lines": 2, "num_statements": 3',
        )
        doesnt_contain("coverage.json", "bad.j2")

    def test_lcov(self) -> None:
        self.make_files()
        cov = coverage.Coverage()
        cov.load()
        cov.lcov_report()
        with open("coverage.lcov", encoding="utf-8") as lcov:
            actual = lcov.read()
        expected = textwrap.dedent("""\
            SF:good.j2
            DA:1,1
            DA:2,0
            DA:3,1
            LF:3
            LH:2
            end_of_record
            """)
        assert expected == actual


class ReportJobsTest(CoverageTest):
    """Reports are the same whether files are analyzed in one process or many."""

    def make_project(self, branch: bool) -> None:
        """Measure a few files, with code that exercises the analysis."""
        self.make_file(
            "proj/mod1.py",
            """\
            def f(x):
                if x:
                    return [
                        1,
                        2,
                    ]
                with open(__file__) as fh:
                    for line in fh:
                        if "never" in line:
                            break
                return None

            class C:
                def m(self, a):
                    return a if a else -a  # pragma: no branch

            f(1)
            f(0)
            """,
        )
        self.make_file("proj/sub/mod2.py", "x = 1\nif x:\n    y = 2\nelse:\n    y = 3\n")
        self.make_file("proj/sub/__init__.py", "")
        self.make_file("proj/main.py", "import mod1\nimport sub.mod2\n")
        self.make_file(".coveragerc", f"[run]\nbranch = {branch}\nsource = proj\n")
        self.run_command("coverage run proj/main.py")

    def make_reports(self, jobs: int) -> dict[str, str]:
        """Write all the reports with `jobs`, and return their contents."""
        cov = coverage.Coverage()
        cov.load()
        cov.set_option("report:jobs", jobs)
        with self.assert_warnings(cov, []):
            with open(f"report{jobs}.txt", "w", encoding="utf-8") as f:
                cov.report(show_missing=True, file=f)
            cov.xml_report(outfile=f"cov{jobs}.xml")
            cov.json_report(outfile=f"cov{jobs}.json", pretty_print=True, show_contexts=True)
            cov.lcov_report(outfile=f"cov{jobs}.lcov")
            cov.html_report(directory=f"html{jobs}")
            cov.annotate(directory=f"ann{jobs}")
        reports = {}
        for name in [f"report{jobs}.txt", f"cov{jobs}.xml", f"cov{jobs}.json", f"cov{jobs}.lcov"]:
            with open(name, encoding="utf-8") as f:
                text = f.read()
            text = re.sub(r'timestamp="\d+"|"timestamp": "[^"]+"', "TIMESTAMP", text)
            reports[name.replace(str(jobs), "")] = text
        for dirname in [f"html{jobs}", f"ann{jobs}"]:
            for fname in sorted(os.listdir(dirname)):
                if fname.endswith((".html", ",cover")):
                    with open(os.path.join(dirname, fname), encoding="utf-8") as f:
                        text = f.read()
                    text = re.sub(r"created at [^<]+", "created at TIME", text)
                    reports[f"{dirname.replace(str(jobs), '')}/{fname}"] = text
        return reports

    @pytest.mark.parametrize("branch", [False, True])
    def test_same_reports(self, branch: bool) -> None:
        self.make_project(branch)
        serial = self.make_reports(1)
        parallel = self.make_reports(2)
        assert serial.keys() == parallel.keys()
        assert len(serial) > 10
        for name, text in serial.items():
            assert text == parallel[name], f"{name} differs"

    def test_same_reports_with_paths(self) -> None:
        self.make_file("src/prog.py", "a = 1\nif a:\n    b = 2\nc = 3\n")
        self.make_data_file(
            arcs={abs_file("elsewhere/prog.py"): arcz_to_arcs(".1 12 23 34 4.")},
        )
        self.make_file(".coveragerc", "[paths]\nsource =\n    src\n    elsewhere\n")
        assert self.make_reports(1) == self.make_reports(2)

    def make_bad_files(self) -> None:
        """Make data for files that can't be analyzed, in different ways."""
        self.make_file("good.py", "a = 1\n")
        self.make_file("bad.py", "a = (\n")
        self.make_file("notpy.j2", "This is {{ data }}.\n")
        self.make_data_file(
            lines={
                abs_file("good.py"): [1],
                abs_file("bad.py"): [1],
                abs_file("notpy.j2"): [1],
                abs_file("zmissing.py"): [1],
            },
        )

    @pytest.mark.parametrize("jobs", [1, 2])
    def test_errors(self, jobs: int) -> None:
        self.make_bad_files()
        cov = coverage.Coverage()
        cov.load()
        cov.set_option("report:jobs", jobs)
        with pytest.raises(NotPython, match="Couldn't parse '.*bad.py' as Python source"):
            cov.xml_report()

    @pytest.mark.parametrize("jobs", [1, 2])
    def test_ignored_errors(self, jobs: int) -> None:
        self.make_bad_files()
        cov = coverage.Coverage()
        cov.load()
        cov.set_option("report:jobs", jobs)
        warnings = [
            r"Couldn't parse Python file '.*bad.py' \(couldnt-parse\)",
            r"Couldn't parse '.*zmissing.py': No source for code: .* \(couldnt-parse\)",
        ]
        with self.assert_warnings(cov, warnings, not_warnings=["no-parallel"]):
            cov.xml_report(ignore_errors=True)
        contains("coverage.xml", 'filename="good.py"')
        doesnt_contain("coverage.xml", "bad.py", "notpy.j2", "zmissing.py")

    @pytest.mark.parametrize(
        "target, replacement",
        [
            (
                "concurrent.futures.ProcessPoolExecutor.submit",
                mock.Mock(side_effect=RuntimeError("no processes")),
            ),
            ("coverage.report_core._analyze_in_worker", lambda *args: None),
        ],
    )
    def test_no_parallel(self, target: str, replacement: object) -> None:
        self.make_project(branch=True)
        cov = coverage.Coverage()
        cov.load()
        cov.xml_report(outfile="serial.xml")
        cov.set_option("report:jobs", 2)
        warning = r"Couldn't analyze files in parallel: .* \(no-parallel\)"
        with mock.patch(target, replacement):
            with self.assert_warnings(cov, [warning]):
                cov.xml_report(outfile="parallel.xml")
        with open("serial.xml", encoding="utf-8") as f:
            serial = re.sub(r'timestamp="\d+"', "", f.read())
        with open("parallel.xml", encoding="utf-8") as f:
            parallel = re.sub(r'timestamp="\d+"', "", f.read())
        assert serial == parallel

    @pytest.mark.parametrize(
        "jobs, expected",
        [
            (0, 1),
            ("1", 1),
            (3, 3),
            ("12", 12),
            ("auto", os.cpu_count() or 1),
            (" AUTO ", os.cpu_count() or 1),
        ],
    )
    def test_report_jobs(self, jobs: int | str, expected: int) -> None:
        assert report_jobs(jobs) == expected

    @pytest.mark.parametrize("jobs", ["-1", -2, "many", "1.5"])
    def test_bad_jobs(self, jobs: int | str) -> None:
        msg = r"\[report\] jobs must be 'auto' or a non-negative integer, not"
        with pytest.raises(ConfigError, match=msg):
            report_jobs(jobs)
