#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8 ff=unix ft=python ts=4 sw=4 sts=4 si et
"""
pip-licenses

MIT License

Copyright (c) 2025 pbhandari

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import argparse
import codecs
import os
import re
import subprocess
import sys
from collections import Counter
from enum import Enum, auto
from functools import partial
from importlib import metadata as importlib_metadata
from importlib.metadata import Distribution
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Type, cast

import tomli
from prettytable import ALL as RULE_ALL
from prettytable import HEADER as RULE_HEADER
from prettytable import NONE as RULE_NONE
from prettytable import PrettyTable

if TYPE_CHECKING:
    from email.message import Message
    from typing import Callable, Dict, Iterator, Optional, Sequence


open = open  # noqa:A001 allow monkey patching

__pkgname__ = "pip-license-audit"
__version__ = "0.0.1"
__summary__ = (
    "Dump the software license list of Python packages installed with pip."
)


FIELD_NAMES = (
    "Name",
    "Version",
    "License",
    "LicenseFile",
    "LicenseText",
    "NoticeFile",
    "NoticeText",
    "Author",
    "Maintainer",
    "Description",
    "URL",
)


SUMMARY_FIELD_NAMES = (
    "Count",
    "License",
)


DEFAULT_OUTPUT_FIELDS = (
    "Name",
    "Version",
)


SUMMARY_OUTPUT_FIELDS = (
    "Count",
    "License",
)


def extract_homepage(metadata: Message) -> Optional[str]:
    """Extracts the homepage attribute from the package metadata.

    Not all python packages have defined a home-page attribute.
    As a fallback, the `Project-URL` metadata can be used.
    The python core metadata supports multiple (free text) values for
    the `Project-URL` field that are comma separated.

    Args:
        metadata: The package metadata to extract the homepage from.

    Returns:
        The home page if applicable, None otherwise.
    """
    homepage = metadata.get("home-page", None)
    if homepage is not None:
        return homepage

    candidates: Dict[str, str] = {}

    for entry in metadata.get_all("Project-URL", []):
        key, value = entry.split(",", 1)
        candidates[key.strip().lower()] = value.strip()

    for priority_key in [
        "homepage",
        "source",
        "repository",
        "changelog",
        "bug tracker",
    ]:
        if priority_key in candidates:
            return candidates[priority_key]

    return None


PATTERN_DELIMITER = re.compile(r"[-_.]+")


def normalize_pkg_name(pkg_name: str) -> str:
    """Return normalized name according to PEP specification

    See here: https://peps.python.org/pep-0503/#normalized-names

    Args:
        pkg_name: Package name it is extracted from the package metadata
                  or specified in the CLI

    Returns:
        normalized packege name
    """
    return PATTERN_DELIMITER.sub("-", pkg_name).lower()


METADATA_KEYS: Dict[str, List[Callable[[Message], Optional[str]]]] = {
    "home-page": [extract_homepage],
    "author": [
        lambda metadata: metadata.get("author"),
        lambda metadata: metadata.get("author-email"),
    ],
    "maintainer": [
        lambda metadata: metadata.get("maintainer"),
        lambda metadata: metadata.get("maintainer-email"),
    ],
    # PyPI doesn't let you set both license and license-expression, they're
    # equivalent so just collapsing them into one.
    "license": [
        lambda metadata: metadata.get("license-expression")
        or metadata.get("license")
    ],
    "summary": [lambda metadata: metadata.get("summary")],
}

# Mapping of FIELD_NAMES to METADATA_KEYS where they differ by more than case
FIELDS_TO_METADATA_KEYS = {
    "URL": "home-page",
    "Description": "summary",
    "License-Metadata": "license",
    "License-Classifier": "license_classifier",
    "License-Expression": "license",
}


SYSTEM_PACKAGES = (
    __pkgname__,
    "pip",
    "prettytable",
    "wcwidth",
    "setuptools",
    "tomli",
    "wheel",
)

LICENSE_UNKNOWN = "UNKNOWN"


def get_packages(
    args: CustomNamespace,
) -> Iterator[dict[str, str | list[str]]]:
    def get_pkg_included_file(
        pkg: Distribution, file_names_rgx: str
    ) -> tuple[str, str]:
        """
        Attempt to find the package's included file on disk and return the
        tuple (included_file_path, included_file_contents).
        """
        included_file = LICENSE_UNKNOWN
        included_text = LICENSE_UNKNOWN

        pkg_files = pkg.files or ()
        pattern = re.compile(file_names_rgx)
        matched_rel_paths = filter(
            lambda file: pattern.match(file.name), pkg_files
        )
        for rel_path in matched_rel_paths:
            abs_path = Path(pkg.locate_file(rel_path)) # type: ignore[arg-type]
            if not abs_path.is_file():
                continue
            included_file = str(abs_path)
            with open(
                abs_path, encoding="utf-8", errors="backslashreplace"
            ) as included_file_handle:
                included_text = included_file_handle.read()
            break
        return (included_file, included_text)

    def get_pkg_info(pkg: Distribution) -> dict[str, str | list[str]]:
        (license_file, license_text) = get_pkg_included_file(
            pkg, "LICEN[CS]E.*|COPYING.*"
        )
        (notice_file, notice_text) = get_pkg_included_file(pkg, "NOTICE.*")
        pkg_info: dict[str, str | list[str]] = {
            "name": pkg.metadata["name"],
            "version": pkg.version,
            "namever": "{} {}".format(pkg.metadata["name"], pkg.version),
            "licensefile": license_file,
            "licensetext": license_text,
            "noticefile": notice_file,
            "noticetext": notice_text,
        }
        metadata = pkg.metadata
        for field_name, field_selector_fns in METADATA_KEYS.items():
            value = None
            for field_selector_fn in field_selector_fns:
                # Type hint of `Distribution.metadata` states `PackageMetadata`
                # but it's actually of type `email.Message`
                value = field_selector_fn(metadata) # type: ignore[arg-type]
                if value:
                    break
            pkg_info[field_name] = value or LICENSE_UNKNOWN

        classifiers: list[str] = metadata.get_all("classifier", [])
        pkg_info["license_classifier"] = find_license_from_classifier(
            classifiers
        )

        if args.filter_strings:

            def filter_string(item: str) -> str:
                return item.encode(
                    args.filter_code_page, errors="ignore"
                ).decode(args.filter_code_page)

            for k in pkg_info:
                if isinstance(pkg_info[k], list):
                    pkg_info[k] = list(map(filter_string, pkg_info[k]))
                else:
                    pkg_info[k] = filter_string(cast(str, pkg_info[k]))

        return pkg_info

    def get_python_sys_path(executable: str) -> list[str]:
        script = "import sys; print(' '.join(filter(bool, sys.path)))"
        output = subprocess.run(
            [executable, "-c", script],
            capture_output=True,
            env={**os.environ, "PYTHONPATH": "", "VIRTUAL_ENV": ""},
        )
        return output.stdout.decode().strip().split()

    if args.python == sys.executable:
        search_paths = sys.path
    else:
        search_paths = get_python_sys_path(args.python)

    pkgs = importlib_metadata.distributions(path=search_paths)
    ignore_pkgs_as_normalize = [
        normalize_pkg_name(pkg) for pkg in args.ignore_packages
    ]
    pkgs_as_normalize = [normalize_pkg_name(pkg) for pkg in args.packages]

    fail_on_licenses = set()
    if args.fail_on:
        fail_on_licenses = set(map(str.strip, args.fail_on.split(";")))

    allow_only_licenses = set()
    if args.allow_only:
        allow_only_licenses = set(map(str.strip, args.allow_only.split(";")))

    for pkg in pkgs:
        pkg_name = normalize_pkg_name(pkg.metadata["name"])
        pkg_name_and_version = pkg_name + ":" + pkg.metadata["version"]

        if (
            pkg_name.lower() in ignore_pkgs_as_normalize
            or pkg_name_and_version.lower() in ignore_pkgs_as_normalize
        ):
            continue

        if pkgs_as_normalize and pkg_name.lower() not in pkgs_as_normalize:
            continue

        if not args.with_system and pkg_name in SYSTEM_PACKAGES:
            continue

        pkg_info = get_pkg_info(pkg)

        license_names = select_license_by_source(
            args.from_,
            cast(List[str], pkg_info["license_classifier"]),
            cast(str, pkg_info["license"]),
        )

        if fail_on_licenses:
            failed_licenses = set()
            if not args.partial_match:
                failed_licenses = case_insensitive_set_intersect(
                    license_names, fail_on_licenses
                )
            else:
                failed_licenses = case_insensitive_partial_match_set_intersect(
                    license_names, fail_on_licenses
                )
            if failed_licenses:
                sys.stderr.write(
                    "fail-on license {} was found for package "
                    "{}:{}\n".format(
                        "; ".join(sorted(failed_licenses)),
                        pkg_info["name"],
                        pkg_info["version"],
                    )
                )
                sys.exit(1)

        if allow_only_licenses:
            uncommon_licenses = set()
            if not args.partial_match:
                uncommon_licenses = case_insensitive_set_diff(
                    license_names, allow_only_licenses
                )
            else:
                uncommon_licenses = case_insensitive_partial_match_set_diff(
                    license_names, allow_only_licenses
                )

            if len(uncommon_licenses) == len(license_names):
                sys.stderr.write(
                    "license {} not in allow-only licenses was found"
                    " for package {}:{}\n".format(
                        "; ".join(sorted(uncommon_licenses)),
                        pkg_info["name"],
                        pkg_info["version"],
                    )
                )
                sys.exit(1)

        yield pkg_info


def create_licenses_table(
    args: CustomNamespace,
    output_fields: Iterable[str] = DEFAULT_OUTPUT_FIELDS,
) -> PrettyTable:
    table = factory_styled_table_with_args(args, output_fields)

    for pkg in get_packages(args):
        row = []
        for field in output_fields:
            if field == "License":
                license_set = select_license_by_source(
                    args.from_,
                    cast(List[str], pkg["license_classifier"]),
                    cast(str, pkg["license"]),
                )
                license_str = "; ".join(sorted(license_set))
                row.append(license_str)
            elif field == "License-Classifier":
                row.append(
                    "; ".join(sorted(pkg["license_classifier"]))
                    or LICENSE_UNKNOWN
                )
            elif field.lower() in pkg:
                row.append(cast(str, pkg[field.lower()]))
            else:
                row.append(cast(str, pkg[FIELDS_TO_METADATA_KEYS[field]]))
        table.add_row(row)

    return table


def create_summary_table(args: CustomNamespace) -> PrettyTable:
    counts = Counter(
        "; ".join(
            sorted(
                select_license_by_source(
                    args.from_,
                    cast(List[str], pkg["license_classifier"]),
                    cast(str, pkg["license"]),
                )
            )
        )
        for pkg in get_packages(args)
    )

    table = factory_styled_table_with_args(args, SUMMARY_FIELD_NAMES)
    for lic, count in counts.items():
        table.add_row([count, lic])
    return table


def case_insensitive_set_intersect(set_a, set_b):
    """Same as set.intersection() but case-insensitive"""
    common_items = set()
    set_b_lower = {item.lower() for item in set_b}
    for elem in set_a:
        if elem.lower() in set_b_lower:
            common_items.add(elem)
    return common_items


def case_insensitive_partial_match_set_intersect(set_a, set_b):
    common_items = set()
    for item_a in set_a:
        for item_b in set_b:
            if item_b.lower() in item_a.lower():
                common_items.add(item_a)
    return common_items


def case_insensitive_partial_match_set_diff(set_a, set_b):
    uncommon_items = set_a.copy()
    for item_a in set_a:
        for item_b in set_b:
            if item_b.lower() in item_a.lower():
                uncommon_items.remove(item_a)
    return uncommon_items


def case_insensitive_set_diff(set_a, set_b):
    """Same as set.difference() but case-insensitive"""
    uncommon_items = set()
    set_b_lower = {item.lower() for item in set_b}
    for elem in set_a:
        if elem.lower() not in set_b_lower:
            uncommon_items.add(elem)
    return uncommon_items


class JsonPrettyTable(PrettyTable):
    """PrettyTable-like class exporting to JSON"""

    def _format_row(self, row: Iterable[str]) -> dict[str, str | list[str]]:
        resrow: dict[str, str | List[str]] = {}
        for field, value in zip(self._field_names, row):
            resrow[field] = value

        return resrow

    def get_string(self, **kwargs: str | list[str]) -> str:
        # import included here in order to limit dependencies
        # if not interested in JSON output,
        # then the dependency is not required
        import json

        options = self._get_options(kwargs)
        rows = self._get_rows(options)
        formatted_rows = self._format_rows(rows)

        lines = []
        for row in formatted_rows:
            lines.append(row)

        return json.dumps(lines, indent=2, sort_keys=True)


class JsonLicenseFinderTable(JsonPrettyTable):
    def _format_row(self, row: Iterable[str]) -> dict[str, str | list[str]]:
        resrow: dict[str, str | List[str]] = {}
        for field, value in zip(self._field_names, row):
            if field == "Name":
                resrow["name"] = value

            if field == "Version":
                resrow["version"] = value

            if field == "License":
                resrow["licenses"] = [value]

        return resrow

    def get_string(self, **kwargs: str | list[str]) -> str:
        # import included here in order to limit dependencies
        # if not interested in JSON output,
        # then the dependency is not required
        import json

        options = self._get_options(kwargs)
        rows = self._get_rows(options)
        formatted_rows = self._format_rows(rows)

        lines = []
        for row in formatted_rows:
            lines.append(row)

        return json.dumps(lines, sort_keys=True)


class CSVPrettyTable(PrettyTable):
    """PrettyTable-like class exporting to CSV"""

    def get_string(self, **kwargs: str | list[str]) -> str:
        def esc_quotes(val: bytes | str) -> str:
            """
            Meta-escaping double quotes
            https://tools.ietf.org/html/rfc4180
            """
            try:
                return cast(str, val).replace('"', '""')
            except UnicodeDecodeError:  # pragma: no cover
                return cast(bytes, val).decode("utf-8").replace('"', '""')
            except UnicodeEncodeError:  # pragma: no cover
                return str(
                    cast(str, val).encode("unicode_escape").replace('"', '""')  # type: ignore[arg-type] # noqa: E501
                )

        options = self._get_options(kwargs)
        rows = self._get_rows(options)
        formatted_rows = self._format_rows(rows)

        lines = []
        formatted_header = ",".join(
            ['"%s"' % (esc_quotes(val),) for val in self._field_names]
        )
        lines.append(formatted_header)
        for row in formatted_rows:
            formatted_row = ",".join(
                ['"%s"' % (esc_quotes(val),) for val in row]
            )
            lines.append(formatted_row)

        return "\n".join(lines)


class PlainVerticalTable(PrettyTable):
    """PrettyTable for outputting to a simple non-column based style.

    When used with --with-license-file, this style is similar to the default
    style generated from Angular CLI's --extractLicenses flag.
    """

    def get_string(self, **kwargs: str | list[str]) -> str:
        options = self._get_options(kwargs)
        rows = self._get_rows(options)

        output = ""
        for row in rows:
            for v in row:
                output += "{}\n".format(v)
            output += "\n"

        return output


def factory_styled_table_with_args(
    args: CustomNamespace,
    output_fields: Iterable[str] = DEFAULT_OUTPUT_FIELDS,
) -> PrettyTable:
    table = PrettyTable()
    table.field_names = output_fields  # type: ignore[assignment]
    table.align = "l"  # type: ignore[assignment]
    table.border = args.format_ in (
        FormatArg.MARKDOWN,
        FormatArg.RST,
        FormatArg.CONFLUENCE,
        FormatArg.JSON,
    )
    table.header = True

    if args.format_ == FormatArg.MARKDOWN:
        table.junction_char = "|"
        table.hrules = RULE_HEADER
    elif args.format_ == FormatArg.RST:
        table.junction_char = "+"
        table.hrules = RULE_ALL
    elif args.format_ == FormatArg.CONFLUENCE:
        table.junction_char = "|"
        table.hrules = RULE_NONE
    elif args.format_ == FormatArg.JSON:
        table = JsonPrettyTable(table.field_names)
    elif args.format_ == FormatArg.JSON_LICENSE_FINDER:
        table = JsonLicenseFinderTable(table.field_names)
    elif args.format_ == FormatArg.CSV:
        table = CSVPrettyTable(table.field_names)
    elif args.format_ == FormatArg.PLAIN_VERTICAL:
        table = PlainVerticalTable(table.field_names)

    return table


def find_license_from_classifier(classifiers: list[str]) -> list[str]:
    licenses = []
    for classifier in filter(lambda c: c.startswith("License"), classifiers):
        lic = classifier.split(" :: ")[-1]

        # Through the declaration of 'Classifier: License :: OSI Approved'
        if lic != "OSI Approved":
            licenses.append(lic)

    return licenses


def select_license_by_source(
    from_source: FromArg, license_classifier: list[str], license_meta: str
) -> set[str]:
    license_classifier_set = set(license_classifier) or {LICENSE_UNKNOWN}
    if (
        from_source == FromArg.CLASSIFIER
        or from_source == FromArg.MIXED
        and len(license_classifier) > 0
    ):
        return license_classifier_set
    else:
        return {license_meta}


def get_output_fields(args: CustomNamespace) -> list[str]:
    if args.summary:
        return list(SUMMARY_OUTPUT_FIELDS)

    output_fields = list(DEFAULT_OUTPUT_FIELDS)

    if args.from_ == FromArg.ALL:
        output_fields.append("License-Metadata")
        output_fields.append("License-Classifier")
    else:
        output_fields.append("License")

    if args.with_authors:
        output_fields.append("Author")

    if args.with_maintainers:
        output_fields.append("Maintainer")

    if args.with_urls:
        output_fields.append("URL")

    if args.with_description:
        output_fields.append("Description")

    if args.no_version:
        output_fields.remove("Version")

    if args.with_license_file:
        if not args.no_license_path:
            output_fields.append("LicenseFile")

        output_fields.append("LicenseText")

        if args.with_notice_file:
            output_fields.append("NoticeText")
            if not args.no_license_path:
                output_fields.append("NoticeFile")

    return output_fields


def get_sortby(args: CustomNamespace) -> str:
    if args.summary and args.order == OrderArg.COUNT:
        return "Count"
    elif args.summary or args.order == OrderArg.LICENSE:
        return "License"
    elif args.order == OrderArg.NAME:
        return "Name"
    elif args.order == OrderArg.AUTHOR and args.with_authors:
        return "Author"
    elif args.order == OrderArg.MAINTAINER and args.with_maintainers:
        return "Maintainer"
    elif args.order == OrderArg.URL and args.with_urls:
        return "URL"

    return "Name"


def create_output_string(args: CustomNamespace) -> str:
    output_fields = get_output_fields(args)

    if args.summary:
        table = create_summary_table(args)
    else:
        table = create_licenses_table(args, output_fields)

    sortby = get_sortby(args)

    if args.format_ == FormatArg.HTML:
        html = table.get_html_string(fields=output_fields, sortby=sortby)
        return html.encode("ascii", errors="xmlcharrefreplace").decode("ascii")
    else:
        return table.get_string(fields=output_fields, sortby=sortby)


def create_warn_string(args: CustomNamespace) -> str:
    warn_messages = []
    warn = partial(output_colored, "33")

    if args.with_license_file and not args.format_ == FormatArg.JSON:
        message = warn(
            (
                "Due to the length of these fields, this option is "
                "best paired with --format=json."
            )
        )
        warn_messages.append(message)

    if args.summary and (args.with_authors or args.with_urls):
        message = warn(
            (
                "When using this option, only --order=count or "
                "--order=license has an effect for the --order "
                "option. And using --with-authors and --with-urls "
                "will be ignored."
            )
        )
        warn_messages.append(message)

    return "\n".join(warn_messages)


class CustomHelpFormatter(argparse.HelpFormatter):  # pragma: no cover
    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 24,
        width: Optional[int] = None,
    ) -> None:
        max_help_position = 30
        super().__init__(
            prog,
            indent_increment=indent_increment,
            max_help_position=max_help_position,
            width=width,
        )

    def _format_action(self, action: argparse.Action) -> str:
        flag_indent_argument: bool = False
        text = self._expand_help(action)
        separator_pos = text[:3].find("|")
        if separator_pos != -1 and "I" in text[:separator_pos]:
            self._indent()
            flag_indent_argument = True
        help_str = super()._format_action(action)
        if flag_indent_argument:
            self._dedent()
        return help_str

    def _expand_help(self, action: argparse.Action) -> str:
        if isinstance(action.default, Enum):
            default_value = enum_key_to_value(action.default)
            return cast(str, self._get_help_string(action)) % {
                "default": default_value
            }
        return super()._expand_help(action)

    def _split_lines(self, text: str, width: int) -> List[str]:
        separator_pos = text[:3].find("|")
        if separator_pos != -1:
            flag_splitlines: bool = "R" in text[:separator_pos]
            text = text[separator_pos + 1:]  # fmt: skip
            if flag_splitlines:
                return text.splitlines()
        return super()._split_lines(text, width)


class CustomNamespace(argparse.Namespace):
    from_: "FromArg"
    order: "OrderArg"
    format_: "FormatArg"
    summary: bool
    output_file: str
    ignore_packages: List[str]
    packages: List[str]
    with_system: bool
    with_authors: bool
    with_urls: bool
    with_description: bool
    with_license_file: bool
    no_license_path: bool
    with_notice_file: bool
    filter_strings: bool
    filter_code_page: str
    partial_match: bool
    fail_on: Optional[str]
    allow_only: Optional[str]


class CompatibleArgumentParser(argparse.ArgumentParser):
    def parse_args(  # type: ignore[override]
        self,
        args: None | Sequence[str] = None,
        namespace: None | CustomNamespace = None,
    ) -> CustomNamespace:
        args_ = cast(CustomNamespace, super().parse_args(args, namespace))
        self._verify_args(args_)
        return args_

    def _verify_args(self, args: CustomNamespace) -> None:
        if args.with_license_file is False and (
            args.no_license_path is True or args.with_notice_file is True
        ):
            self.error(
                "'--no-license-path' and '--with-notice-file' require "
                "the '--with-license-file' option to be set"
            )
        if args.filter_strings is False and args.filter_code_page != "latin1":
            self.error(
                "'--filter-code-page' requires the '--filter-strings' "
                "option to be set"
            )
        try:
            codecs.lookup(args.filter_code_page)
        except LookupError:
            self.error(
                "invalid code page '%s' given for '--filter-code-page, "
                "check https://docs.python.org/3/library/codecs.html"
                "#standard-encodings for valid code pages"
                % args.filter_code_page
            )


class NoValueEnum(Enum):
    def __repr__(self) -> str:  # pragma: no cover
        return "<%s.%s>" % (self.__class__.__name__, self.name)


class FromArg(NoValueEnum):
    META = M = auto()
    CLASSIFIER = C = auto()
    MIXED = MIX = auto()
    ALL = auto()


class OrderArg(NoValueEnum):
    COUNT = C = auto()
    LICENSE = L = auto()
    NAME = N = auto()
    AUTHOR = A = auto()
    MAINTAINER = M = auto()
    URL = U = auto()


class FormatArg(NoValueEnum):
    PLAIN = P = auto()
    PLAIN_VERTICAL = auto()
    MARKDOWN = MD = M = auto()
    RST = REST = R = auto()
    CONFLUENCE = C = auto()
    HTML = H = auto()
    JSON = J = auto()
    JSON_LICENSE_FINDER = JLF = auto()
    CSV = auto()


def value_to_enum_key(value: str) -> str:
    return value.replace("-", "_").upper()


def enum_key_to_value(enum_key: Enum) -> str:
    return enum_key.name.replace("_", "-").lower()


def choices_from_enum(enum_cls: Type[NoValueEnum]) -> List[str]:
    return [
        key.replace("_", "-").lower() for key in enum_cls.__members__.keys()
    ]


def get_value_from_enum(
    enum_cls: Type[NoValueEnum], value: str
) -> NoValueEnum:
    return getattr(enum_cls, value_to_enum_key(value))


MAP_DEST_TO_ENUM = {
    "from_": FromArg,
    "order": OrderArg,
    "format_": FormatArg,
}


class SelectAction(argparse.Action):
    def __call__(  # type: ignore[override]
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: Optional[str] = None,
    ) -> None:
        enum_cls = MAP_DEST_TO_ENUM[self.dest]
        setattr(namespace, self.dest, get_value_from_enum(enum_cls, values))


def load_config_from_file(pyproject_path: str):
    if Path(pyproject_path).exists():
        with open(pyproject_path, "rb") as f:
            return tomli.load(f).get("tool", {}).get(__pkgname__, {})
    return {}


def create_parser(
    pyproject_path: str = "pyproject.toml",
) -> CompatibleArgumentParser:
    parser = CompatibleArgumentParser(
        description=__summary__, formatter_class=CustomHelpFormatter
    )

    config_from_file = load_config_from_file(pyproject_path)

    common_options = parser.add_argument_group("Common options")
    format_options = parser.add_argument_group("Format options")
    verify_options = parser.add_argument_group("Verify options")

    parser.add_argument(
        "-v", "--version", action="version", version="%(prog)s " + __version__
    )

    common_options.add_argument(
        "--python",
        type=str,
        default=config_from_file.get("python", sys.executable),
        metavar="PYTHON_EXEC",
        help="R| path to python executable to search distributions from\n"
        "Package will be searched in the selected python's sys.path\n"
        "By default, will search packages for current env executable\n"
        "(default: sys.executable)",
    )

    common_options.add_argument(
        "--from",
        dest="from_",
        action=SelectAction,
        type=str,
        default=get_value_from_enum(
            FromArg, config_from_file.get("from", "mixed")
        ),
        metavar="SOURCE",
        choices=choices_from_enum(FromArg),
        help="R|where to find license information\n"
        '"meta", "classifier, "mixed", "all"\n'
        "(default: %(default)s)",
    )
    common_options.add_argument(
        "-o",
        "--order",
        action=SelectAction,
        type=str,
        default=get_value_from_enum(
            OrderArg, config_from_file.get("order", "name")
        ),
        metavar="COL",
        choices=choices_from_enum(OrderArg),
        help="R|order by column\n"
        '"name", "license", "author", "url"\n'
        "(default: %(default)s)",
    )
    common_options.add_argument(
        "-f",
        "--format",
        dest="format_",
        action=SelectAction,
        type=str,
        default=get_value_from_enum(
            FormatArg, config_from_file.get("format", "plain")
        ),
        metavar="STYLE",
        choices=choices_from_enum(FormatArg),
        help="R|dump as set format style\n"
        '"plain", "plain-vertical" "markdown", "rst", \n'
        '"confluence", "html", "json", \n'
        '"json-license-finder",  "csv"\n'
        "(default: %(default)s)",
    )
    common_options.add_argument(
        "--summary",
        action="store_true",
        default=config_from_file.get("summary", False),
        help="dump summary of each license",
    )
    common_options.add_argument(
        "--output-file",
        action="store",
        default=config_from_file.get("output-file"),
        type=str,
        help="save license list to file",
    )
    common_options.add_argument(
        "-i",
        "--ignore-packages",
        action="store",
        type=str,
        nargs="+",
        metavar="PKG",
        default=config_from_file.get("ignore-packages", []),
        help="ignore package name in dumped list",
    )
    common_options.add_argument(
        "-p",
        "--packages",
        action="store",
        type=str,
        nargs="+",
        metavar="PKG",
        default=config_from_file.get("packages", []),
        help="only include selected packages in output",
    )
    format_options.add_argument(
        "-s",
        "--with-system",
        action="store_true",
        default=config_from_file.get("with-system", False),
        help="dump with system packages",
    )
    format_options.add_argument(
        "-a",
        "--with-authors",
        action="store_true",
        default=config_from_file.get("with-authors", False),
        help="dump with package authors",
    )
    format_options.add_argument(
        "--with-maintainers",
        action="store_true",
        default=config_from_file.get("with-maintainers", False),
        help="dump with package maintainers",
    )
    format_options.add_argument(
        "-u",
        "--with-urls",
        action="store_true",
        default=config_from_file.get("with-urls", False),
        help="dump with package urls",
    )
    format_options.add_argument(
        "-d",
        "--with-description",
        action="store_true",
        default=config_from_file.get("with-description", False),
        help="dump with short package description",
    )
    format_options.add_argument(
        "-nv",
        "--no-version",
        action="store_true",
        default=config_from_file.get("no-version", False),
        help="dump without package version",
    )
    format_options.add_argument(
        "-l",
        "--with-license-file",
        action="store_true",
        default=config_from_file.get("with-license-file", False),
        help="dump with location of license file and "
        "contents, most useful with JSON output",
    )
    format_options.add_argument(
        "--no-license-path",
        action="store_true",
        default=config_from_file.get("no-license-path", False),
        help="I|when specified together with option -l, "
        "suppress location of license file output",
    )
    format_options.add_argument(
        "--with-notice-file",
        action="store_true",
        default=config_from_file.get("with-notice-file", False),
        help="I|when specified together with option -l, "
        "dump with location of license file and contents",
    )
    format_options.add_argument(
        "--filter-strings",
        action="store_true",
        default=config_from_file.get("filter-strings", False),
        help="filter input according to code page",
    )
    format_options.add_argument(
        "--filter-code-page",
        action="store",
        type=str,
        default=config_from_file.get("filter-code-page", "latin1"),
        metavar="CODE",
        help="I|specify code page for filtering " "(default: %(default)s)",
    )

    verify_options.add_argument(
        "--fail-on",
        action="store",
        type=str,
        default=config_from_file.get("fail-on", None),
        help="fail (exit with code 1) on the first occurrence "
        "of the licenses of the semicolon-separated list",
    )
    verify_options.add_argument(
        "--allow-only",
        action="store",
        type=str,
        default=config_from_file.get("allow-only", None),
        help="fail (exit with code 1) on the first occurrence "
        "of the licenses not in the semicolon-separated list",
    )
    verify_options.add_argument(
        "--partial-match",
        action="store_true",
        default=config_from_file.get("partial-match", False),
        help="enables partial matching for --allow-only/--fail-on",
    )

    return parser


def output_colored(code: str, text: str, is_bold: bool = False) -> str:
    """
    Create function to output with color sequence
    """
    if is_bold:
        code = "1;%s" % code

    return "\033[%sm%s\033[0m" % (code, text)


def save_if_needs(output_file: None | str, output_string: str) -> None:
    """
    Save to path given by args
    """
    if output_file is None:
        return

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output_string)
            if not output_string.endswith("\n"):
                # Always end output files with a new line
                f.write("\n")

        sys.stdout.write("created path: " + output_file + "\n")
        sys.exit(0)
    except IOError:
        sys.stderr.write("check path: --output-file\n")
        sys.exit(1)


def main() -> None:  # pragma: no cover
    parser = create_parser()
    args = parser.parse_args()

    output_string = create_output_string(args)

    output_file = args.output_file
    save_if_needs(output_file, output_string)

    print(output_string)
    warn_string = create_warn_string(args)
    if warn_string:
        print(warn_string, file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    main()
