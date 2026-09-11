"""
File and file system-related tools, specifically for
  * listing directory contents
  * reading files
  * creating files
  * editing at the file level
"""

import os
import re
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from serena.errors import UserFacingError
from serena.result_metadata import ResultIdentityText
from serena.tools import SUCCESS_RESULT, EditedFileContext, EditingToolWithDiagnostics, Tool
from serena.util.file_system import scan_directory
from serena.util.text_utils import (
    ContentReplacer,
    GlobMatcher,
    MultiFileContentReplacer,
    ReplacementOccurrence,
)
from solidlsp.ls_utils import TextUtils


class ReadFileTool(Tool):
    """
    Reads a file within the project directory.
    """

    def apply(self, relative_path: str, start_line: int = 0, end_line: int | None = None) -> str:
        """Reads the given file or an exact line range.

        :param relative_path: the relative path to the file to read
        :param start_line: the 0-based first line, with negative values counting from the end
        :param end_line: the inclusive 0-based final line, or ``None`` for the rest of the file
        :return: the complete requested file content
        """
        if end_line is not None and end_line < 0:
            raise UserFacingError("end_line must be non-negative when provided.")
        if start_line >= 0 and end_line is not None and end_line < start_line:
            raise UserFacingError("end_line must be greater than or equal to start_line.")

        result_lines = TextUtils.split_lines(self.project.read_file(relative_path))
        if start_line < -len(result_lines) or (start_line >= len(result_lines) and result_lines):
            raise UserFacingError(f"start_line {start_line} is outside the file's {len(result_lines)} lines.")

        selected = result_lines[start_line:] if end_line is None else result_lines[start_line : end_line + 1]
        return "\n".join(selected)


class CreateTextFileTool(EditingToolWithDiagnostics):
    """
    Creates/overwrites a file in the project directory.
    """

    def apply(self, relative_path: str, content: str) -> str | dict[str, object]:
        """Writes a new file or overwrites an existing file.

        :param relative_path: the relative path to the file to create
        :param content: the UTF-8-compatible text to write
        :return: ``OK`` with an overwrite qualifier when applicable, plus new diagnostics when present
        """
        with self.DiagnosticsContext(self, relative_path) as diagnostics_context:
            project_root = Path(self.get_project_root())
            abs_path = (project_root / relative_path).resolve()
            if not abs_path.is_relative_to(project_root):
                raise UserFacingError(f"Path must stay within the active project: {relative_path}")
            will_overwrite_existing = abs_path.exists()
            if will_overwrite_existing:
                self.project.validate_relative_path(relative_path)
                if not abs_path.is_file():
                    raise UserFacingError(f"Destination is not a regular file: {relative_path}")

            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(content, encoding=self.project.project_config.encoding, newline=self.project.line_ending.newline_str)
            except OSError as error:
                raise UserFacingError(f"Could not write {relative_path}: {error.strerror or error}") from None
            self.project.ls_sync_file_system_changes((relative_path,))

            result = "OK; overwrote existing file" if will_overwrite_existing else SUCCESS_RESULT
            return diagnostics_context.format_result(result)


class ListDirTool(Tool):
    """
    Lists files and directories in the given directory (optionally with recursion).
    """

    def apply(self, relative_path: str, recursive: bool, skip_ignored_files: bool = False) -> dict[str, list[str]]:
        """Lists files and directories under one project-relative directory.

        :param relative_path: the directory to list; pass ``.`` for the project root
        :param recursive: whether to recurse into subdirectories
        :param skip_ignored_files: whether ignored paths should be omitted
        :return: a native object containing directory and file paths
        """
        self.project.validate_relative_path(relative_path)
        abs_path = Path(self.get_project_root()) / relative_path
        if not abs_path.exists():
            raise UserFacingError(f"Directory not found: {relative_path}")
        if not abs_path.is_dir():
            raise UserFacingError(f"Expected a directory path, got a file: {relative_path}")

        is_ignored_path_fn = self.project.get_is_ignored_path_fn(relative_path, skip_ignored_files)
        dirs, files = scan_directory(
            str(abs_path),
            relative_to=self.get_project_root(),
            recursive=recursive,
            is_ignored_dir=is_ignored_path_fn,
            is_ignored_file=is_ignored_path_fn,
        )
        return {
            "dirs": [ResultIdentityText(path) for path in dirs],
            "files": [ResultIdentityText(path) for path in files],
        }


class FindFileTool(Tool):
    """
    Finds files in the given relative paths
    """

    def apply(self, file_mask: str, relative_path: str) -> dict[str, list[str]]:
        """Finds files matching one filename mask below a project-relative directory.

        :param file_mask: filename mask using ``*`` and ``?`` wildcards
        :param relative_path: directory to search; pass ``.`` for the project root
        :return: a native object containing matching file paths
        """
        self.project.validate_relative_path(relative_path)
        abs_path = Path(self.get_project_root()) / relative_path
        if not abs_path.exists():
            raise UserFacingError(f"Search root does not exist: {relative_path}")
        if not abs_path.is_dir():
            raise UserFacingError(f"Expected a directory search root, got a file: {relative_path}")

        is_ignored_path_fn = self.project.get_is_ignored_path_fn(relative_path, skip_ignored_paths=False)

        def is_ignored_file(candidate: str) -> bool:
            if is_ignored_path_fn(candidate):
                return True
            return not fnmatch(os.path.basename(candidate), file_mask)

        _dirs, files = scan_directory(
            path=str(abs_path),
            recursive=True,
            is_ignored_dir=is_ignored_path_fn,
            is_ignored_file=is_ignored_file,
            relative_to=self.get_project_root(),
        )
        return {"files": [ResultIdentityText(path) for path in files]}


class ReplaceContentTool(EditingToolWithDiagnostics):
    """
    Replaces content in a file (optionally using regular expressions).
    """

    def apply(
        self,
        relative_path: str,
        needle: str,
        repl: str,
        mode: Literal["literal", "regex"],
        allow_multiple_occurrences: bool = False,
    ) -> str | dict[str, object]:
        r"""
        Replaces one or more occurrences of a given pattern in a file with new content.

        Regex mode uses DOTALL and MULTILINE semantics and can match spans with bounded wildcards such as
        "beginning.*?end-of-text-to-be-replaced". Ambiguous single-match replacements fail without modifying the file.
        Use this for small edits inside a symbol or file-level text that is not itself a complete named symbol. Do not replace
        an entire named function, method, class, or other symbol with this tool; retrieve it and use ``replace_symbol_body``.

        :param relative_path: the relative path to the file
        :param needle: the string or regex pattern to search for.
            If `mode` is "literal", this string will be matched exactly.
            If `mode` is "regex", this string will be treated as a regular expression (syntax of Python's `re` module,
            with flags DOTALL and MULTILINE enabled).
        :param repl: the replacement string (verbatim).
            If mode is "regex", the string can contain backreferences to matched groups in the needle regex,
            specified using the syntax $!1, $!2, etc. for groups 1, 2, etc.
        :param mode: either "literal" or "regex", specifying how the `needle` parameter is to be interpreted.
        :param allow_multiple_occurrences: whether to allow matching and replacing multiple occurrences.
            If false and multiple occurrences are found, an error will be returned
        """
        with self.DiagnosticsContext(self, relative_path) as diagnostics_context:
            self.project.validate_relative_path(relative_path)
            replacer = ContentReplacer(mode=mode, allow_multiple_occurrences=allow_multiple_occurrences)

            # perform file-level edits directly so the tool does not require semantic analysis
            original_content = self.project.read_file(relative_path)
            updated_content = replacer.replace(original_content, needle, repl)
            abs_path = Path(self.get_project_root()) / relative_path
            try:
                abs_path.write_text(
                    updated_content,
                    encoding=self.project.project_config.encoding,
                    newline=self.project.line_ending.newline_str,
                )
            except OSError as error:
                raise UserFacingError(f"Could not write {relative_path}: {error.strerror or error}") from None
            self.project.ls_sync_file_system_changes((relative_path,))

            return diagnostics_context.format_result(SUCCESS_RESULT)


class ReplaceInFilesTool(EditingToolWithDiagnostics):
    """
    Replaces occurrences of a pattern across multiple files, with dry-run preview and per-occurrence selection.
    """

    def apply(
        self,
        needle: str,
        repl: str,
        mode: Literal["literal", "regex"],
        relative_path: str = "",
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        dry_run: bool = False,
        occurrence_ids: list[str] | None = None,
        expected_count: int = -1,
    ) -> str | dict[str, object]:
        r"""
        Replaces occurrences of a pattern across multiple files in ONE call.

        This is the preferred tool for repeated small edits (renames, import swaps, annotation changes,
        path prefixes) spanning several files or many places in one file: one call with a SHORT pattern
        replaces many single-file replacements with long disambiguating needles.

        Recommended protocol whenever there is ANY risk of unintended replacements:
        1. Call with dry_run=True: every prospective change is returned as a minimal line diff with an
           occurrence id; nothing is modified.
        2. Call again with dry_run=False, passing the ids you want in occurrence_ids (omit it to apply
           all). You pick the desired replacements from the list - no counting, no needle-crafting.

        For clearly unambiguous bulk replacements you may skip the dry run; pass expected_count as a
        guard. If the actual number of matches differs, NOTHING is changed and the diff list is
        returned, so a failed guard costs one call and gives you the dry-run output to select from.

        :param needle: the string (mode "literal") or regular expression (mode "regex"; Python `re`
            syntax with DOTALL and MULTILINE) to search for
        :param repl: the replacement string. In regex mode, backreferences to matched groups can be
            specified as $!1, $!2, etc.
        :param mode: either "literal" or "regex", specifying how `needle` is to be interpreted
        :param relative_path: only consider this file or directory (default: the whole project)
        :param paths_include_glob: optional glob (relative to the project root, e.g. "src/**/*.cpp")
            restricting which files are considered
        :param paths_exclude_glob: optional glob of files to exclude; takes precedence over the include glob
        :param dry_run: if True, do not modify anything; return the prospective changes as a list of
            diffs with occurrence ids
        :param occurrence_ids: optional list of occurrence ids (obtained from a dry run) to which the
            replacement is restricted; if any id is unknown or stale, NOTHING is changed. If omitted,
            all occurrences are replaced.
        :param expected_count: optional guard for calls without occurrence_ids: the number of
            occurrences you expect to be replaced. If the actual count differs, nothing is changed and
            the list of prospective changes is returned. -1 disables the guard.
        :return: in a dry run, the prospective changes; otherwise a summary of the applied replacements
        """
        replacer = MultiFileContentReplacer(mode=mode)
        files = self._collect_files(relative_path, paths_include_glob, paths_exclude_glob)
        occurrences = replacer.find_occurrences(files, needle, repl)
        contents = dict(files)

        if dry_run:
            return self._render_listing(replacer, occurrences, contents, dry_run=True)

        if occurrence_ids is not None:
            selected, problems = self._resolve_occurrence_ids(occurrence_ids, occurrences)
            if problems:
                problem_lines = "\n".join(f"  {p}" for p in problems)
                raise UserFacingError(
                    f"{len(problems)} occurrence id(s) could not be resolved; no changes were applied:\n"
                    f"{problem_lines}\nRe-run with dry_run=True to obtain current occurrence ids."
                )
            if not selected:
                raise UserFacingError("occurrence_ids is empty; pass at least one id from a dry run or omit it to replace all occurrences.")
            return self._apply_occurrences(replacer, selected, contents, needle, repl)

        # blind apply (no ids)
        if not occurrences:
            raise UserFacingError(
                "No occurrences of the pattern were found; no changes were applied. Check the mode and path/glob restrictions."
            )
        if expected_count >= 0 and len(occurrences) != expected_count:
            listing = self._render_listing(replacer, occurrences, contents, dry_run=False)
            raise UserFacingError(
                f"expected_count={expected_count}, but the pattern matches {len(occurrences)} occurrence(s); no changes were applied.\n{listing}"
            )
        ambiguous = [o for o in occurrences if o.is_ambiguous]
        if ambiguous:
            listing = self._render_listing(replacer, occurrences, contents, dry_run=False)
            raise UserFacingError(
                f"{len(ambiguous)} occurrence(s) are ambiguous; no changes were applied. Refine the pattern or select explicit occurrence_ids.\n{listing}"
            )
        return self._apply_occurrences(replacer, occurrences, contents, needle, repl)

    def _collect_files(self, relative_path: str, paths_include_glob: str, paths_exclude_glob: str) -> list[tuple[str, str]]:
        """Collects readable non-ignored files in deterministic path order."""
        relative_path = relative_path.strip()
        if relative_path:
            self.project.validate_relative_path(relative_path, require_not_ignored=True)
        abs_path = Path(self.get_project_root()) / relative_path
        if not abs_path.exists():
            raise UserFacingError(f"Relative path does not exist: {relative_path or '.'}")

        if abs_path.is_file():
            rel_paths = [relative_path]
        else:
            _dirs, rel_paths = scan_directory(
                path=str(abs_path),
                recursive=True,
                is_ignored_dir=self.project.is_ignored_path,
                is_ignored_file=self.project.is_ignored_path,
                relative_to=self.get_project_root(),
            )
        include_glob_matcher = GlobMatcher(paths_include_glob.strip()) if paths_include_glob.strip() else None
        exclude_glob_matcher = GlobMatcher(paths_exclude_glob.strip()) if paths_exclude_glob.strip() else None
        files: list[tuple[str, str]] = []
        for path in sorted(rel_paths):
            if include_glob_matcher and not include_glob_matcher.matches(path):
                continue
            if exclude_glob_matcher and exclude_glob_matcher.matches(path):
                continue
            try:
                files.append((path, self.project.read_file(path)))
            except UserFacingError:
                continue
        return files

    def _render_listing(
        self,
        replacer: MultiFileContentReplacer,
        occurrences: list[ReplacementOccurrence],
        contents: dict[str, str],
        dry_run: bool,
    ) -> str:
        """Renders the complete prospective replacement listing."""
        affected_files = sorted({o.relative_path for o in occurrences})
        header = f"Found {len(occurrences)} occurrence(s) in {len(affected_files)} file(s)."
        if dry_run:
            header += (
                " DRY RUN - no changes were applied.\n"
                "Re-issue with dry_run=False to replace all of them, or additionally pass occurrence_ids "
                "with the ids of the occurrences to replace."
            )
        parts = [header]
        for path in affected_files:
            file_occurrences = [o for o in occurrences if o.relative_path == path]
            parts.append(f"\n{path} ({len(file_occurrences)} occurrence(s)):")
            for occ in file_occurrences:
                parts.append(replacer.render_occurrence_diff(occ, contents[path]))
        return "\n".join(parts)

    @staticmethod
    def _resolve_occurrence_ids(
        occurrence_ids: list[str], occurrences: list[ReplacementOccurrence]
    ) -> tuple[list[ReplacementOccurrence], list[str]]:
        """Resolves the requested ids against the current occurrences, diagnosing each failure."""
        occurrences_by_id = {o.occurrence_id: o for o in occurrences}
        indices_by_path: dict[str, set[int]] = {}
        for o in occurrences:
            indices_by_path.setdefault(o.relative_path, set()).add(o.index_in_file)
        selected: dict[str, ReplacementOccurrence] = {}
        problems: list[str] = []
        for oid in occurrence_ids:
            occurrence = occurrences_by_id.get(oid)
            if occurrence is not None:
                selected[oid] = occurrence
                continue
            id_match = MultiFileContentReplacer.OCCURRENCE_ID_REGEX.match(oid)
            if id_match is None:
                problems.append(f"{oid}: malformed id (expected '<path>:<index>@<digest>' as returned by a dry run)")
            elif id_match.group("path") not in indices_by_path:
                problems.append(f"{oid}: the pattern currently has no matches in this file")
            elif int(id_match.group("index")) not in indices_by_path[id_match.group("path")]:
                problems.append(f"{oid}: the file now has fewer matches than at dry-run time (content changed)")
            else:
                problems.append(f"{oid}: the matched text changed since the dry run (content changed)")
        return list(selected.values()), problems

    def _apply_occurrences(
        self,
        replacer: MultiFileContentReplacer,
        occurrences: list[ReplacementOccurrence],
        contents: dict[str, str],
        needle: str,
        repl: str,
    ) -> str | dict[str, object]:
        occurrences_by_file: dict[str, list[ReplacementOccurrence]] = {}
        for occ in occurrences:
            occurrences_by_file.setdefault(occ.relative_path, []).append(occ)
        with self.DiagnosticsContext(self, *occurrences_by_file.keys()) as diagnostics_context:
            code_editor = self.create_code_editor()
            for path, file_occurrences in occurrences_by_file.items():
                with EditedFileContext(path, code_editor) as context:
                    original_content = context.get_original_content()
                    if original_content != contents[path]:
                        # the editor's view differs from what was scanned (e.g. line-ending normalization);
                        # re-derive the occurrences from the authoritative content and re-validate by id
                        fresh_by_id = {o.occurrence_id: o for o in replacer.find_occurrences([(path, original_content)], needle, repl)}
                        try:
                            file_occurrences = [fresh_by_id[o.occurrence_id] for o in file_occurrences]
                        except KeyError as error:
                            raise UserFacingError(
                                f"The content of {path} changed while replacing (occurrence {error} no longer resolves); "
                                "the file was not modified. Re-run with dry_run=True for current ids."
                            ) from None
                    context.set_updated_content(replacer.apply_to_content(original_content, file_occurrences))
            per_file = "\n".join(f"  {path}: {len(occs)}" for path, occs in occurrences_by_file.items())
            summary = f"Replaced {len(occurrences)} occurrence(s) in {len(occurrences_by_file)} file(s):\n{per_file}"
            return diagnostics_context.format_result(summary)


class SearchForPatternTool(Tool):
    def apply(
        self,
        substring_pattern: str,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        relative_path: str = "",
        restrict_search_to_code_files: bool = False,
        skip_ignored_files: bool = True,
        multiline: bool = True,
    ) -> dict[str, list[str]]:
        """
        Searches for a regex pattern across project files, returning whole matched lines (plus optional context).
        Use this for arbitrary text/non-symbol structure or discovery when a target code symbol cannot yet be identified
        semantically. Do not use it to read the body of a named class, function, method, constructor, or other analyzable code
        symbol; use ``find_symbol`` (or ``get_symbols_overview`` first when the file structure is unfamiliar). If a pattern search
        discovers a candidate symbol, continue with symbolic retrieval rather than expanding regex context to read its implementation.

        :param substring_pattern: regular expression to search for.
        :param context_lines_before: number of context lines to include before each match.
        :param context_lines_after: number of context lines to include after each match.
        :param paths_include_glob: optional glob (relative to project root, e.g. ``"src/**/*.ts"``) restricting which files are searched.
        :param paths_exclude_glob: optional glob to exclude files; takes precedence over `paths_include_glob`.
        :param relative_path: restricts the search to this file or subdirectory of the project root
        :param restrict_search_to_code_files: whether to search only (non-ignored) files containing analyzable code symbols;
            otherwise also search non-code files.
        :param skip_ignored_files: whether to skip ignored sub-paths (default: True)
        :param multiline: whether to apply multi-line matching (default: True), enabling the flags re.DOTALL and re.MULTILINE
        :return: a native mapping from file paths to complete matched consecutive lines (0-based line numbers).
        """
        relative_path = relative_path.strip()
        if relative_path:
            self.project.validate_relative_path(relative_path)
        if context_lines_before < 0 or context_lines_after < 0:
            raise UserFacingError("context_lines_before and context_lines_after must be non-negative.")
        try:
            re.compile(substring_pattern, flags=(re.MULTILINE | re.DOTALL) if multiline else re.MULTILINE)
        except re.error as error:
            raise UserFacingError(f"Invalid regex: {error}") from None
        if paths_include_glob.strip():
            GlobMatcher(paths_include_glob.strip())
        if paths_exclude_glob.strip():
            GlobMatcher(paths_exclude_glob.strip())

        matches = self.project.search_project_files_for_pattern(
            pattern=substring_pattern,
            relative_path=relative_path,
            context_lines_before=context_lines_before,
            context_lines_after=context_lines_after,
            paths_include_glob=paths_include_glob.strip(),
            paths_exclude_glob=paths_exclude_glob.strip(),
            multiline=multiline,
            code_files_only=restrict_search_to_code_files,
            skip_ignored_files=skip_ignored_files,
        )

        # group unique displayed matches by file so repeated regex hits do not duplicate the same source context
        file_to_matches: dict[str, list[str]] = defaultdict(list)
        seen_displays_by_file: dict[str, set[str]] = defaultdict(set)
        for match in matches:
            assert match.source_file_path is not None
            path = match.source_file_path
            display = match.to_display_string()
            if display not in seen_displays_by_file[path]:
                file_to_matches[path].append(display)
                seen_displays_by_file[path].add(display)

        return dict(file_to_matches)

    """
    Performs a search for a pattern in the project.
    """

    """
    Performs a search for a pattern in the project.
    """
