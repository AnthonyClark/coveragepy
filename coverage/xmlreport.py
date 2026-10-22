# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""XML reporting for coverage.py"""

# pragma: enforce 100% coverage

from __future__ import annotations

import os
import os.path
import sys
import time
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from coverage import env, files
from coverage.misc import human_sorted, human_sorted_items, isolate_module, neutralize_control
from coverage.plugin import FileReporter
from coverage.report_core import get_analysis_to_report
from coverage.results import Analysis
from coverage.types import TMorfs
from coverage.version import __url__, __version__

if TYPE_CHECKING:
    from coverage import Coverage

os = isolate_module(os)


DTD_URL = "https://raw.githubusercontent.com/cobertura/web/master/htdocs/xml/coverage-04.dtd"


def rate(hit: int, num: int) -> str:
    """Return the fraction of `hit`/`num`, as a string.

    Note that "0" is only returned when `hit` is truly zero, and "1"
    is only returned when `hit == num` (or `num == 0`). Rounding can never
    result in either "0" or "1".
    """
    if num == 0:
        # No lines at all counts as 100%
        return "1"
    elif hit == num:
        return "1"
    elif hit == 0:
        return "0"
    else:
        frac = min(max(hit / num, 0.0001), 0.9999)
        return f"{frac:.4g}"


def xml_text(text: str) -> str:
    """Escape `text` for XML character data, the same as xml.dom.minidom."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if env.PYBEHAVIOR.minidom_quotes_text:
        text = text.replace('"', "&quot;")
    return text


def xml_attr(text: str) -> str:
    """Escape `text` for an XML attribute value, the same as xml.dom.minidom."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    if not env.PYBEHAVIOR.minidom_quotes_text:
        text = text.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")
    return text


def xml_tag(depth: int, tag: str, attrs: dict[str, str] | None = None, empty: bool = False) -> str:
    """Produce an XML start tag on its own line indented to `depth`, or an empty element."""
    attr_text = "".join(f' {name}="{xml_attr(value)}"' for name, value in (attrs or {}).items())
    end = "/>" if empty else ">"
    return "\t" * depth + f"<{tag}{attr_text}{end}\n"


def xml_end_tag(depth: int, tag: str) -> str:
    """Produce an XML end tag on its own line indented to `depth`."""
    return "\t" * depth + f"</{tag}>\n"


@dataclass
class PackageData:
    """Data we keep about each "package" (in Java terms)."""

    elements: dict[str, str]
    hits: int
    lines: int
    br_hits: int
    branches: int


class XmlReporter:
    """A reporter for writing Cobertura-style XML coverage results."""

    report_type = "XML report"

    def __init__(self, coverage: Coverage) -> None:
        self.coverage = coverage
        self.config = self.coverage.config

        self.source_paths = set()
        if self.config.source:
            for src in self.config.source:
                if os.path.exists(src):
                    if self.config.relative_files:
                        src = src.rstrip(r"\/")
                    else:
                        src = files.canonical_filename(src)
                    self.source_paths.add(src)
        self.packages: dict[str, PackageData] = {}

    def report(self, morfs: TMorfs, outfile: IO[str] | None = None) -> float:
        """Generate a Cobertura-compatible XML report for `morfs`.

        `morfs` is a list of modules or file names.

        `outfile` is a file object to write the XML to.

        """
        # Initial setup.
        outfile = outfile or sys.stdout
        has_arcs = self.coverage.get_data().has_arcs()
        timestamp = str(int(time.time() * 1000))

        # Call xml_file for each file in the data.
        for fr, analysis in get_analysis_to_report(self.coverage, morfs):
            self.xml_file(fr, analysis, has_arcs)

        lnum_tot, lhits_tot = 0, 0
        bnum_tot, bhits_tot = 0, 0
        xpackages = []
        for pkg_name, pkg_data in human_sorted_items(self.packages.items()):
            if has_arcs:
                branch_rate = rate(pkg_data.br_hits, pkg_data.branches)
            else:
                branch_rate = "0"
            package_attrs = {
                "name": pkg_name.replace(os.sep, "."),
                "line-rate": rate(pkg_data.hits, pkg_data.lines),
                "branch-rate": branch_rate,
                "complexity": "0",
            }
            xpackages.append(xml_tag(2, "package", package_attrs))
            xpackages.append(xml_tag(3, "classes"))
            xpackages.extend(
                class_xml for _, class_xml in human_sorted_items(pkg_data.elements.items())
            )
            xpackages.append(xml_end_tag(3, "classes"))
            xpackages.append(xml_end_tag(2, "package"))

            lhits_tot += pkg_data.hits
            lnum_tot += pkg_data.lines
            bhits_tot += pkg_data.br_hits
            bnum_tot += pkg_data.branches

        coverage_attrs = {
            "version": __version__,
            "timestamp": timestamp,
            "lines-valid": str(lnum_tot),
            "lines-covered": str(lhits_tot),
            "line-rate": rate(lhits_tot, lnum_tot),
        }
        if has_arcs:
            coverage_attrs["branches-valid"] = str(bnum_tot)
            coverage_attrs["branches-covered"] = str(bhits_tot)
            coverage_attrs["branch-rate"] = rate(bhits_tot, bnum_tot)
        else:
            coverage_attrs["branches-covered"] = "0"
            coverage_attrs["branches-valid"] = "0"
            coverage_attrs["branch-rate"] = "0"
        coverage_attrs["complexity"] = "0"

        sources = [
            "\t" * 2 + f"<source>{xml_text(neutralize_control(path))}</source>\n"
            for path in human_sorted(self.source_paths)
        ]

        # Write the output file.
        outfile.write('<?xml version="1.0" ?>\n')
        outfile.write(xml_tag(0, "coverage", coverage_attrs))
        outfile.write(f"\t<!-- Generated by coverage.py: {__url__} -->\n")
        outfile.write(f"\t<!-- Based on {DTD_URL} -->\n")
        if sources:
            outfile.write(xml_tag(1, "sources"))
            outfile.write("".join(sources))
            outfile.write(xml_end_tag(1, "sources"))
        else:
            outfile.write(xml_tag(1, "sources", empty=True))
        if xpackages:
            outfile.write(xml_tag(1, "packages"))
            outfile.write("".join(xpackages))
            outfile.write(xml_end_tag(1, "packages"))
        else:
            outfile.write(xml_tag(1, "packages", empty=True))
        outfile.write(xml_end_tag(0, "coverage"))

        # Return the total percentage.
        denom = lnum_tot + bnum_tot
        if denom == 0:
            pct = 0.0
        else:
            pct = 100.0 * (lhits_tot + bhits_tot) / denom
        return pct

    def xml_file(self, fr: FileReporter, analysis: Analysis, has_arcs: bool) -> None:
        """Add to the XML report for a single file."""

        if self.config.skip_empty:
            if analysis.numbers.n_statements == 0:
                return

        # Create the "lines" and "package" XML elements, which
        # are populated later.  Note that a package == a directory.
        filename = fr.filename.replace("\\", "/")
        for source_path in self.source_paths:
            if not self.config.relative_files:
                source_path = files.canonical_filename(source_path)
            if filename.startswith(source_path.replace("\\", "/") + "/"):
                rel_name = filename[len(source_path) + 1 :]
                break
        else:
            rel_name = fr.relative_filename().replace("\\", "/")
            self.source_paths.add(fr.filename[: -len(rel_name)].rstrip(r"\/"))

        rel_name = neutralize_control(rel_name)

        dirname = os.path.dirname(rel_name) or "."
        dirname = "/".join(dirname.split("/")[: self.config.xml_package_depth])
        package_name = dirname.replace("/", ".")

        package = self.packages.setdefault(package_name, PackageData({}, 0, 0, 0, 0))

        branch_stats = analysis.branch_stats()
        missing_branch_arcs = analysis.missing_branch_arcs()

        # For each statement, create an XML "line" element.
        xlines = []
        for line in sorted(analysis.statements):
            # Q: can we get info about the number of times a statement is
            # executed?  If so, that should be recorded here.
            line_attrs = {"number": str(line), "hits": str(int(line not in analysis.missing))}

            if has_arcs:
                if line in branch_stats:
                    total, taken = branch_stats[line]
                    line_attrs["branch"] = "true"
                    line_attrs["condition-coverage"] = f"{100 * taken // total}% ({taken}/{total})"
                if line in missing_branch_arcs:
                    annlines = ["exit" if b < 0 else str(b) for b in missing_branch_arcs[line]]
                    line_attrs["missing-branches"] = ",".join(annlines)
            xlines.append(xml_tag(6, "line", line_attrs, empty=True))

        class_lines = len(analysis.statements)
        class_hits = class_lines - len(analysis.missing)

        if has_arcs:
            class_branches = sum(t for t, k in branch_stats.values())
            missing_branches = sum(t - k for t, k in branch_stats.values())
            class_br_hits = class_branches - missing_branches
        else:
            class_branches = 0
            class_br_hits = 0

        # Finalize the statistics that are collected in the XML.
        if has_arcs:
            branch_rate = rate(class_br_hits, class_branches)
        else:
            branch_rate = "0"
        class_attrs = {
            "name": os.path.relpath(rel_name, dirname),
            "filename": rel_name.replace("\\", "/"),
            "complexity": "0",
            "line-rate": rate(class_hits, class_lines),
            "branch-rate": branch_rate,
        }
        xclass = [xml_tag(4, "class", class_attrs)]
        xclass.append(xml_tag(5, "methods", empty=True))
        if xlines:
            xclass.append(xml_tag(5, "lines"))
            xclass.extend(xlines)
            xclass.append(xml_end_tag(5, "lines"))
        else:
            xclass.append(xml_tag(5, "lines", empty=True))
        xclass.append(xml_end_tag(4, "class"))

        package.elements[rel_name] = "".join(xclass)
        package.hits += class_hits
        package.lines += class_lines
        package.br_hits += class_br_hits
        package.branches += class_branches
