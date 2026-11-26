"""Pass 1 orchestration: name every module, summarize files in parallel, cache the summaries."""

from __future__ import annotations

import hashlib
import os
import pickle
from dataclasses import dataclass
from pathlib import Path

from kaszanas_lint import __version__
from kaszanas_lint.configuration import LintConfiguration
from kaszanas_lint.file_discovery import relative_posix_path
from kaszanas_lint.module_summarization import summarize_module_source
from kaszanas_lint.parallel_execution import map_possibly_in_parallel
from kaszanas_lint.source_text import read_python_source
from kaszanas_lint.symbols import ModuleSummary

CACHE_DIRECTORY_NAME = ".kaszanas_lint_cache"
CACHE_FILE_NAME = "module-summaries-v1.pickle"
CACHE_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceFile:
    absolute_path: Path
    relative_path: str  # posix, relative to the project root
    module_name: str
    is_package: bool


def _normalized_root_prefixes(source_roots: tuple[Path, ...], project_root: Path) -> list[str]:
    """Resolved roots as normcase'd string prefixes ending in a separator, longest first."""
    prefixes = {os.path.join(os.path.normcase(root.resolve()), "") for root in (*source_roots, project_root)}
    return sorted(prefixes, key=len, reverse=True)


def module_identity(file_path: Path, root_prefixes: list[str]) -> tuple[str, bool]:
    """Dotted module name from the most specific source root containing an absolute, resolved path."""
    normalized_path = os.path.normcase(file_path)
    relative_path: str | None = None
    for prefix in root_prefixes:
        if normalized_path.startswith(prefix):
            relative_path = str(file_path)[len(prefix) :]
            break
    if relative_path is None:
        relative_parts = [file_path.stem]
    else:
        relative_parts = os.path.splitext(relative_path)[0].replace(os.sep, "/").split("/")
    is_package = bool(relative_parts) and relative_parts[-1] == "__init__"
    if is_package:
        relative_parts = relative_parts[:-1]
    return ".".join(relative_parts), is_package


def build_source_files(file_paths: list[Path], configuration: LintConfiguration) -> list[SourceFile]:
    source_files: list[SourceFile] = []
    claimed_module_names: set[str] = set()
    root_prefixes = _normalized_root_prefixes(
        source_roots=configuration.source_roots, project_root=configuration.project_root
    )
    for file_path in file_paths:
        module_name, is_package = module_identity(file_path=file_path, root_prefixes=root_prefixes)
        relative_path = relative_posix_path(file_path=file_path, project_root=configuration.project_root)
        if module_name in claimed_module_names:
            # Two files claim one dotted name (e.g. scripts without packages); keep it checkable
            # but unreachable by imports so it can't shadow the first one.
            module_name = f"<duplicate>.{relative_path}"
        claimed_module_names.add(module_name)
        source_files.append(
            SourceFile(
                absolute_path=file_path,
                relative_path=relative_path,
                module_name=module_name,
                is_package=is_package,
            )
        )
    return source_files


def _summarize_source_file(source_file: SourceFile, safe_decorators: frozenset[str]) -> ModuleSummary:
    try:
        source = read_python_source(file_path=source_file.absolute_path).text
    except (OSError, UnicodeDecodeError, SyntaxError) as read_error:
        return ModuleSummary(
            module_name=source_file.module_name,
            relative_path=source_file.relative_path,
            is_package=source_file.is_package,
            syntax_error=f"could not read file: {read_error}",
        )
    return summarize_module_source(
        source=source,
        module_name=source_file.module_name,
        relative_path=source_file.relative_path,
        is_package=source_file.is_package,
        safe_decorators=safe_decorators,
    )


def _summarize_task(task: tuple[SourceFile, frozenset[str]]) -> ModuleSummary:
    source_file, safe_decorators = task
    return _summarize_source_file(source_file=source_file, safe_decorators=safe_decorators)


def _configuration_fingerprint(configuration: LintConfiguration) -> str:
    material = "|".join(
        [__version__, *sorted(configuration.safe_decorators), *map(str, configuration.source_roots)]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ModuleSummaryCache:
    """mtime+size keyed pickle of summaries; invalidated wholesale when configuration changes."""

    def __init__(self, configuration: LintConfiguration) -> None:
        self.cache_directory = configuration.project_root / CACHE_DIRECTORY_NAME
        self.cache_file = self.cache_directory / CACHE_FILE_NAME
        self.fingerprint = _configuration_fingerprint(configuration=configuration)
        self.entries: dict[str, tuple[int, int, ModuleSummary]] = {}

    def load(self) -> None:
        try:
            with self.cache_file.open(mode="rb") as cache_handle:
                payload = pickle.load(cache_handle)
        except (OSError, pickle.PickleError, EOFError, AttributeError, ImportError, TypeError):
            return
        if (
            isinstance(payload, dict)
            and payload.get("format_version") == CACHE_FORMAT_VERSION
            and payload.get("configuration_fingerprint") == self.fingerprint
        ):
            self.entries = payload.get("summaries", {})

    def lookup(self, source_file: SourceFile) -> ModuleSummary | None:
        entry = self.entries.get(source_file.relative_path)
        if entry is None:
            return None
        cached_mtime, cached_size, summary = entry
        try:
            status = source_file.absolute_path.stat()
        except OSError:
            return None
        if (status.st_mtime_ns, status.st_size) != (cached_mtime, cached_size):
            return None
        if (summary.module_name, summary.is_package) != (
            source_file.module_name,
            source_file.is_package,
        ):
            return None
        return summary

    def store(self, source_files: list[SourceFile], summaries: dict[str, ModuleSummary]) -> None:
        refreshed: dict[str, tuple[int, int, ModuleSummary]] = {}
        for source_file in source_files:
            summary = summaries.get(source_file.module_name)
            if summary is None:
                continue
            try:
                status = source_file.absolute_path.stat()
            except OSError:
                continue
            refreshed[source_file.relative_path] = (status.st_mtime_ns, status.st_size, summary)
        try:
            self.cache_directory.mkdir(exist_ok=True)
            (self.cache_directory / ".gitignore").write_text("*\n", encoding="utf-8")
            temporary_file = self.cache_file.with_suffix(".tmp")
            with temporary_file.open(mode="wb") as cache_handle:
                pickle.dump(
                    {
                        "format_version": CACHE_FORMAT_VERSION,
                        "configuration_fingerprint": self.fingerprint,
                        "summaries": refreshed,
                    },
                    cache_handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            temporary_file.replace(self.cache_file)
        except OSError:
            pass  # a read-only checkout must still lint


def build_project_index(
    source_files: list[SourceFile], configuration: LintConfiguration, jobs: int | None
) -> dict[str, ModuleSummary]:
    cache = ModuleSummaryCache(configuration=configuration)
    if configuration.cache_enabled:
        cache.load()

    summaries: dict[str, ModuleSummary] = {}
    files_to_parse: list[SourceFile] = []
    for source_file in source_files:
        cached_summary = cache.lookup(source_file=source_file) if configuration.cache_enabled else None
        if cached_summary is None:
            files_to_parse.append(source_file)
        else:
            summaries[source_file.module_name] = cached_summary

    fresh_summaries = map_possibly_in_parallel(
        function=_summarize_task,
        tasks=[(source_file, configuration.safe_decorators) for source_file in files_to_parse],
        jobs=jobs,
    )
    for summary in fresh_summaries:
        summaries[summary.module_name] = summary

    if configuration.cache_enabled and files_to_parse:
        cache.store(source_files=source_files, summaries=summaries)
    return summaries
