import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Self

from joblib import Parallel, delayed
from sensai.util.string import ToStringMixin

from serena.errors import UserFacingError
from serena.util.file_proxy import FileCollection, FileProxy
from solidlsp.ls_utils import TextUtils

log = logging.getLogger(__name__)


class LineType(StrEnum):
    """Enum for different types of lines in search results."""

    MATCH = "match"
    """Part of the matched lines"""
    BEFORE_MATCH = "prefix"
    """Lines before the match"""
    AFTER_MATCH = "postfix"
    """Lines after the match"""


@dataclass(kw_only=True)
class TextLine:
    """Represents a line of text with information on how it relates to the match."""

    line_number: int
    line_content: str
    match_type: LineType
    """Represents the type of line (match, prefix, postfix)"""

    def get_display_prefix(self) -> str:
        """Get the display prefix for this line based on the match type."""
        if self.match_type == LineType.MATCH:
            return "  >"
        return "..."

    def format_line(self, include_line_numbers: bool = True) -> str:
        """Format the line for display (e.g.,for logging or passing to an LLM).

        :param include_line_numbers: Whether to include the line number in the result.
        """
        prefix = self.get_display_prefix()
        if include_line_numbers:
            line_num = str(self.line_number).rjust(4)
            prefix = f"{prefix}{line_num}"
        return f"{prefix}:{self.line_content}"


@dataclass(kw_only=True)
class MatchedConsecutiveLines:
    """Represents a collection of consecutive lines found through some criterion in a text file or a string.
    May include lines before, after, and matched.
    """

    lines: list[TextLine]
    """All lines in the context of the match. At least one of them is of `match_type` `MATCH`."""
    source_file_path: str | None = None
    """Path to the file where the match was found (Metadata)."""

    # set in post-init
    lines_before_matched: list[TextLine] = field(default_factory=list)
    matched_lines: list[TextLine] = field(default_factory=list)
    lines_after_matched: list[TextLine] = field(default_factory=list)

    def __post_init__(self) -> None:
        for line in self.lines:
            if line.match_type == LineType.BEFORE_MATCH:
                self.lines_before_matched.append(line)
            elif line.match_type == LineType.MATCH:
                self.matched_lines.append(line)
            elif line.match_type == LineType.AFTER_MATCH:
                self.lines_after_matched.append(line)

        assert len(self.matched_lines) > 0, "At least one matched line is required"

    @property
    def start_line(self) -> int:
        return self.lines[0].line_number

    @property
    def end_line(self) -> int:
        return self.lines[-1].line_number

    @property
    def num_matched_lines(self) -> int:
        return len(self.matched_lines)

    def to_display_string(self, include_line_numbers: bool = True) -> str:
        return "\n".join([line.format_line(include_line_numbers) for line in self.lines])

    @classmethod
    def from_file_contents(
        cls, file_contents: str, line: int, context_lines_before: int = 0, context_lines_after: int = 0, source_file_path: str | None = None
    ) -> Self:
        line_contents = TextUtils.split_lines(file_contents)
        start_lineno = max(0, line - context_lines_before)
        end_lineno = min(len(line_contents) - 1, line + context_lines_after)
        text_lines: list[TextLine] = []
        # before the line
        for lineno in range(start_lineno, line):
            text_lines.append(TextLine(line_number=lineno, line_content=line_contents[lineno], match_type=LineType.BEFORE_MATCH))
        # the line
        text_lines.append(TextLine(line_number=line, line_content=line_contents[line], match_type=LineType.MATCH))
        # after the line
        for lineno in range(line + 1, end_lineno + 1):
            text_lines.append(TextLine(line_number=lineno, line_content=line_contents[lineno], match_type=LineType.AFTER_MATCH))

        return cls(lines=text_lines, source_file_path=source_file_path)


def search_text(
    pattern: str,
    content: str | None = None,
    source_file_path: str | None = None,
    context_lines_before: int = 0,
    context_lines_after: int = 0,
    multiline: bool = True,
) -> list[MatchedConsecutiveLines]:
    """
    Search for a pattern in text content. Supports both regex and glob-like patterns.

    :param pattern: Pattern to search for (regex or glob-like pattern)
    :param content: The text content to search. May be None if source_file_path is provided.
    :param source_file_path: Optional path to the source file. If content is None,
        this has to be passed and the file will be read.
    :param context_lines_before: Number of context lines to include before matches
    :param context_lines_after: Number of context lines to include after matches
    :param multiline: whether to apply multi-line matching, enabling the flags re.DOTALL and re.MULTILINE
    :return: List of `TextSearchMatch` objects
    :raises: ValueError if the pattern is not valid
    """
    if source_file_path and content is None:
        with open(source_file_path) as f:
            content = f.read()

    if content is None:
        raise ValueError("Pass either content or source_file_path")

    matches = []
    lines = TextUtils.split_lines(content)
    total_lines = len(lines)

    # For multiline matches, optionally use DOTALL so '.' matches newlines
    flags = (re.MULTILINE | re.DOTALL) if multiline else 0
    compiled_pattern = re.compile(pattern, flags)
    # Search across the entire content as a single string
    for match in compiled_pattern.finditer(content):
        start_pos = match.start()
        end_pos = match.end()

        # Find the line numbers for the start and end positions
        start_line_num = TextUtils.get_line_from_index(content, start_pos)
        end_line_num = TextUtils.get_line_from_index(content, end_pos)
        if end_line_num > start_line_num and TextUtils.get_line_col_from_index(content, end_pos)[1] == 0:
            # `end_pos` is exclusive, so if it is at the start of a line, the match ends with the
            # preceding line's newline and does not extend into the line that `end_pos` points to
            end_line_num -= 1

        # Calculate the range of lines to include in the context
        context_start = max(0, start_line_num - context_lines_before)
        context_end = min(total_lines - 1, end_line_num + context_lines_after)

        # Create TextLine objects for the context
        context_lines = []
        for line_num in range(context_start, context_end + 1):
            if context_start <= line_num < start_line_num:
                match_type = LineType.BEFORE_MATCH
            elif end_line_num < line_num <= context_end:
                match_type = LineType.AFTER_MATCH
            else:
                match_type = LineType.MATCH

            context_lines.append(TextLine(line_number=line_num, line_content=lines[line_num], match_type=match_type))

        matches.append(MatchedConsecutiveLines(lines=context_lines, source_file_path=source_file_path))

    return matches


class GlobMatcher(ToStringMixin):
    """
    Supports matching a file path against a glob pattern.

    Supports standard glob patterns:
    - * matches any number of characters except /
    - ** matches any number of directories (zero or more)
    - ? matches a single character except /
    - [seq] matches any character in seq

    Supports brace expansion:
    - {a,b,c} expands to multiple patterns (including nesting)
    """

    def __init__(self, expr: str):
        """
        :param expr: a glob pattern which may make use of brace expansion (e.g., "src/**/*.{js,jsx,ts,tsx}")
        """
        expr = expr.replace("\\", "/")  # normalise backslashes to forward slashes
        self._glob_expr = expr
        self._glob_patterns = self._expand_braces(expr)
        self._regex_patterns = [re.compile(self._translate_glob_to_regex(p)) for p in self._glob_patterns]

    def _tostring_includes(self) -> list[str]:
        return ["_glob_expr"]

    @staticmethod
    def _expand_braces(pattern: str) -> list[str]:
        """
        Expands brace patterns in a glob string.
        For example, "**/*.{js,jsx,ts,tsx}" becomes ["**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx"].
        Handles multiple brace sets as well.
        """
        patterns = [pattern]
        while any("{" in p or "}" in p for p in patterns):
            new_patterns = []
            for p in patterns:
                match = re.search(r"\{([^{}]*)\}", p)
                if match:
                    options_expr = match.group(1)
                    if not options_expr:
                        raise UserFacingError(f"Invalid glob brace expression in {pattern!r}: empty braces are not allowed")

                    prefix = p[: match.start()]
                    suffix = p[match.end() :]
                    options = options_expr.split(",")
                    for option in options:
                        new_patterns.append(f"{prefix}{option}{suffix}")
                else:
                    raise UserFacingError(f"Invalid glob brace expression in {pattern!r}: unmatched brace")
            patterns = new_patterns
        return patterns

    @staticmethod
    def _translate_glob_to_regex(pattern: str) -> str:
        res = []
        i = 0
        n = len(pattern)
        while i < n:
            c = pattern[i]
            i += 1
            if c == "*":
                if i < n and pattern[i] == "*":  # **
                    i += 1
                    if i < n and pattern[i] == "/":  # **/
                        i += 1
                        if res and res[-1] == "/":  # /**/
                            res = res[:-1]
                            res.append("(?:/|/.*/)")
                        elif res:  # c**/ (with c != '/')
                            res.append(".*/")
                        else:  # **/ at the start of the pattern
                            res.append("(?:.*/)?")
                    else:  # c**d  (with c, d != '/')
                        res.append(".*")
                else:  # single *
                    if not res or res[-1] != "[^/]*":
                        res.append("[^/]*")
            elif c == "?":
                res.append("[^/]")
            elif c == "[":
                j = i
                if j < n and pattern[j] == "!":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 1
                if j >= n:
                    res.append(r"\[")
                else:
                    stuff = pattern[i:j]
                    if stuff.startswith("!"):
                        stuff = "^" + stuff[1:]
                    elif stuff.startswith("^"):
                        stuff = r"\^" + stuff
                    res.append(f"[{stuff}]")
                    i = j + 1
            else:
                res.append(re.escape(c))
        inner = "".join(res)
        return f"(?s:{inner})\\Z"

    def matches(self, path: str) -> bool:
        path = path.replace("\\", "/")  # normalise backslashes to forward slashes
        return any(regex.match(path) for regex in self._regex_patterns)


def search_files(
    file_collection: FileCollection,
    pattern: str,
    context_lines_before: int = 0,
    context_lines_after: int = 0,
    paths_include_glob: str | None = None,
    paths_exclude_glob: str | None = None,
    multiline: bool = True,
) -> list[MatchedConsecutiveLines]:
    """
    Search for a pattern in a list of files.

    :param file_collection: the collection of files to search (will be optionally filtered by glob patterns)
    :param pattern: pattern to search for
    :param context_lines_before: number of context lines to include before matches
    :param context_lines_after: number of context lines to include after matches
    :param paths_include_glob: optional glob pattern to include files from the list
    :param paths_exclude_glob: optional glob pattern to exclude files from the list
    :param multiline: whether to apply multi-line matching, enabling the flags re.DOTALL and re.MULTILINE (default: True)
    :return: list of MatchedConsecutiveLines objects
    """
    # apply glob filter
    file_collection = file_collection.filter_glob(paths_include_glob=paths_include_glob, paths_exclude_glob=paths_exclude_glob)
    log.info(f"Processing {len(file_collection)} files.")

    def process_single_file(file_proxy: FileProxy) -> dict[str, Any]:
        """Process a single file - this function will be parallelized."""
        relative_path = file_proxy.get_relative_path()
        try:
            file_content = file_proxy.get_contents()
            search_results = search_text(
                pattern,
                content=file_content,
                source_file_path=relative_path,
                context_lines_before=context_lines_before,
                context_lines_after=context_lines_after,
                multiline=multiline,
            )
            if len(search_results) > 0:
                log.debug(f"Found {len(search_results)} matches in {relative_path}")
            return {"path": relative_path, "results": search_results, "error": None}
        except Exception as e:
            log.debug(f"Error processing {relative_path}: {e}")
            return {"path": relative_path, "results": [], "error": str(e)}

    # Execute in parallel using joblib
    results = Parallel(
        n_jobs=-1,
        backend="threading",
    )(delayed(process_single_file)(file_proxy) for file_proxy in file_collection)

    # Collect results and errors
    matches = []
    skipped_file_error_tuples = []

    for result in results:
        if result["error"]:
            skipped_file_error_tuples.append((result["path"], result["error"]))
        else:
            matches.extend(result["results"])

    if skipped_file_error_tuples:
        log.debug(f"Failed to read {len(skipped_file_error_tuples)} files: {skipped_file_error_tuples}")

    log.info(f"Found {len(matches)} total matches across {len(file_collection)} files")
    return matches


class ContentReplacer:
    """
    This is an LLM-optimised content replacer, which elegantly circumvents escaping and which
    provides dual modes for maximum flexibility.
    """

    def __init__(self, mode: Literal["literal", "regex"], allow_multiple_occurrences: bool, regex_multiline: bool = True):
        """

        :param mode: the mode indicating whether to the needle in replacements corresponds to a regular expression
            (mode "regex") or to a literal string (mode "literal")
        :param allow_multiple_occurrences: whether it is allowed that the search expression matches multiple occurrences.
            If False, an error will be raised if more than one match is found.
        :param regex_multiline: whether to apply multi-line regex matching, enabling the flags re.DOTALL and re.MULTILINE
        """
        self.mode = mode
        self.allow_multiple_occurrences = allow_multiple_occurrences
        self.regex_multiline = regex_multiline

    @staticmethod
    def _create_replacement_function(regex_pattern: str, repl_template: str, regex_flags: int) -> Callable[[re.Match], str]:
        """Creates the validated replacement function for one regex pattern."""

        def validate_and_replace(match: re.Match) -> str:
            matched_text = match.group(0)

            # reject a multiline match that contains another match of the same pattern
            if "\n" in matched_text and re.search(regex_pattern, matched_text[1:], flags=regex_flags):
                raise UserFacingError(
                    "Match is ambiguous: the search pattern matches multiple overlapping occurrences. "
                    "Revise the search pattern to be more specific or use literal mode."
                )

            def expand_backreference(backreference: re.Match) -> str:
                group_num = int(backreference.group(1))
                try:
                    group_value = match.group(group_num)
                except IndexError:
                    raise UserFacingError(f"Replacement references missing regex group $!{group_num}.") from None
                return group_value if group_value is not None else backreference.group(0)

            return re.sub(r"\$!(\d+)", expand_backreference, repl_template)

        return validate_and_replace

    def replace(
        self,
        content: str,
        needle: str,
        repl: str,
    ) -> str:
        """Performs one literal or regular-expression replacement.

        :param content: the content in which to perform the replacement
        :param needle: the literal string or regular expression to search for
        :param repl: the replacement text, including optional ``$!N`` regex backreferences
        :return: the updated content
        """
        if self.mode == "literal":
            regex = re.escape(needle)
        elif self.mode == "regex":
            regex = needle
        else:
            raise UserFacingError(f"Invalid mode: {self.mode!r}; expected 'literal' or 'regex'.")

        regex_flags = (re.MULTILINE | re.DOTALL) if self.regex_multiline else 0
        repl_fn = self._create_replacement_function(regex, repl, regex_flags=regex_flags)

        try:
            updated_content, count = re.subn(regex, repl_fn, content, flags=regex_flags)
        except re.error as error:
            raise UserFacingError(f"Invalid regex: {error}") from None

        if count == 0:
            raise UserFacingError("No matches of search expression found.")
        if not self.allow_multiple_occurrences and count > 1:
            raise UserFacingError(
                f"Expression matches {count} occurrences. Revise the expression to be more specific or enable "
                "allow_multiple_occurrences if this is expected."
            )
        return updated_content


@dataclass
class ReplacementOccurrence:
    """A single prospective replacement of a pattern match within one file."""

    occurrence_id: str
    """stable, content-anchored identifier: '<relative_path>:<index_in_file>@<digest>'"""
    relative_path: str
    index_in_file: int
    """0-based index of this match among the matches within its file (in position order)"""
    start: int
    """character offset of the match start within the file content"""
    end: int
    """character offset of the match end within the file content"""
    matched_text: str
    replacement: str
    """the fully expanded replacement text (backreferences already resolved)"""
    start_line: int
    """0-based line number of the match start"""
    end_line: int
    """0-based line number of the match end"""
    is_ambiguous: bool = False
    """whether the pattern matches again within the matched text (possible over-match)"""


class MultiFileContentReplacer:
    """
    Occurrence-level counterpart of :class:`ContentReplacer` operating on multiple files:
    finds every match of a pattern across a set of file contents, assigns each occurrence a
    stable content-anchored id, renders minimal line diffs for previewing, and computes the
    updated content of a file for a selected subset of occurrences.
    """

    OCCURRENCE_ID_REGEX = re.compile(r"^(?P<path>.+):(?P<index>\d+)@(?P<digest>[0-9a-f]{6})$")
    _DIGEST_LEN = 6

    def __init__(self, mode: Literal["literal", "regex"], regex_multiline: bool = True):
        """
        :param mode: whether the needle is a literal string ("literal") or a regular expression ("regex")
        :param regex_multiline: whether to apply multi-line regex matching, enabling the flags re.DOTALL and re.MULTILINE
        """
        if mode not in ("literal", "regex"):
            raise UserFacingError(f"Invalid mode: {mode!r}; expected 'literal' or 'regex'.")
        self.mode = mode
        self._flags = (re.MULTILINE | re.DOTALL) if regex_multiline else 0

    def _compile(self, needle: str) -> re.Pattern:
        try:
            return re.compile(re.escape(needle) if self.mode == "literal" else needle, flags=self._flags)
        except re.error as error:
            raise UserFacingError(f"Invalid regex: {error}") from None

    @classmethod
    def _digest(cls, matched_text: str) -> str:
        return hashlib.sha1(matched_text.encode("utf-8")).hexdigest()[: cls._DIGEST_LEN]

    @classmethod
    def make_occurrence_id(cls, relative_path: str, index_in_file: int, matched_text: str) -> str:
        return f"{relative_path}:{index_in_file}@{cls._digest(matched_text)}"

    @staticmethod
    def _expand_backreferences(match: re.Match, repl_template: str) -> str:
        """Expands ``$!N`` references in one replacement template."""

        def expand(backreference: re.Match) -> str:
            group_num = int(backreference.group(1))
            try:
                group_value = match.group(group_num)
            except IndexError:
                raise UserFacingError(f"Replacement references missing regex group $!{group_num}.") from None
            return group_value if group_value is not None else backreference.group(0)

        return re.sub(r"\$!(\d+)", expand, repl_template)

    def find_occurrences(self, files: list[tuple[str, str]], needle: str, repl: str) -> list[ReplacementOccurrence]:
        """
        Finds all matches of the needle in the given files.

        :param files: (relative_path, content) pairs; processed in the given order
        :param needle: the search expression (literal string or regex, depending on the mode)
        :param repl: the replacement template (may contain $!N backreferences in regex mode)
        :return: occurrences in deterministic order (file order, then position within the file)
        """
        pattern = self._compile(needle)
        occurrences: list[ReplacementOccurrence] = []
        for relative_path, content in files:
            for index_in_file, match in enumerate(pattern.finditer(content)):
                matched_text = match.group(0)
                replacement = self._expand_backreferences(match, repl) if self.mode == "regex" else repl
                # same over-match heuristic as ContentReplacer: for a multi-line match, the pattern
                # matching again within the matched text indicates the match may have swallowed
                # more than intended
                is_ambiguous = "\n" in matched_text and pattern.search(matched_text[1:]) is not None
                occurrences.append(
                    ReplacementOccurrence(
                        occurrence_id=self.make_occurrence_id(relative_path, index_in_file, matched_text),
                        relative_path=relative_path,
                        index_in_file=index_in_file,
                        start=match.start(),
                        end=match.end(),
                        matched_text=matched_text,
                        replacement=replacement,
                        start_line=content.count("\n", 0, match.start()),
                        end_line=content.count("\n", 0, match.end()),
                        is_ambiguous=is_ambiguous,
                    )
                )
        return occurrences

    @staticmethod
    def apply_to_content(content: str, occurrences: list[ReplacementOccurrence]) -> str:
        """
        Applies the given occurrences (which must have been derived from exactly this content)
        and returns the updated content.
        """
        for occ in sorted(occurrences, key=lambda o: o.start, reverse=True):
            assert content[occ.start : occ.end] == occ.matched_text, (
                f"Occurrence {occ.occurrence_id} does not match the content it is being applied to"
            )
            content = content[: occ.start] + occ.replacement + content[occ.end :]
        return content

    @staticmethod
    def _format_block(block: str, prefix: str, max_lines: int, max_line_chars: int) -> list[str]:
        lines = block.split("\n")
        shown = lines[:max_lines]
        result = []
        for line in shown:
            if len(line) > max_line_chars:
                line = line[:max_line_chars] + f"… (+{len(line) - max_line_chars} chars)"
            result.append(f"    {prefix} {line}")
        if len(lines) > max_lines:
            result.append(f"    {prefix} … ({len(lines) - max_lines} more lines)")
        return result

    def render_occurrence_diff(
        self, occ: ReplacementOccurrence, content: str, max_lines_per_side: int = 6, max_line_chars: int = 200
    ) -> str:
        """
        Renders a minimal line diff for the occurrence: the full lines spanned by the match,
        before and after the replacement.

        :param occ: the occurrence (must have been derived from exactly this content)
        :param content: the file content the occurrence was found in
        :param max_lines_per_side: cap on the number of displayed lines per diff side
        :param max_line_chars: cap on the number of displayed characters per line
        :return: the rendered diff
        """
        line_start = content.rfind("\n", 0, occ.start) + 1
        line_end = content.find("\n", occ.end)
        if line_end == -1:
            line_end = len(content)
        old_block = content[line_start:line_end]
        new_block = content[line_start : occ.start] + occ.replacement + content[occ.end : line_end]
        location = f"line {occ.start_line}" if occ.start_line == occ.end_line else f"lines {occ.start_line}-{occ.end_line}"
        header = f"  [{occ.occurrence_id}] {location}"
        if occ.is_ambiguous:
            header += "  (WARNING: the pattern matches again inside this match — possible over-match, verify the diff)"
        diff_lines = [header]
        diff_lines += self._format_block(old_block, "-", max_lines_per_side, max_line_chars)
        diff_lines += self._format_block(new_block, "+", max_lines_per_side, max_line_chars)
        return "\n".join(diff_lines)


@dataclass
class TextCoords:
    line: int
    """
    0-based line number
    """
    col: int
    """
    0-based column number
    """


def find_text_coordinates(content: str, regex: str, require_unique: bool = False) -> TextCoords | None:
    """Finds the line and column of the first regex capture group.

    :param content: the text content to search
    :param regex: a regular expression containing exactly one capture group
    :param require_unique: whether exactly one match is required
    :return: the captured position, or ``None`` when no match is found and uniqueness is not required
    """
    try:
        pattern = re.compile(regex, flags=re.MULTILINE | re.DOTALL)
    except re.error as error:
        raise UserFacingError(f"Invalid regex: {error}") from None

    matches = list(pattern.finditer(content))
    if not matches:
        if require_unique:
            raise UserFacingError("No match found for the supplied regex.")
        return None
    if require_unique and len(matches) > 1:
        raise UserFacingError(f"Match must be unique; found {len(matches)} matches.")

    match = matches[0]
    if len(match.groups()) != 1:
        raise UserFacingError(f"Regex must contain exactly one group; found {len(match.groups())}.")
    index_in_content = match.start(1)
    line, col = TextUtils.get_line_col_from_index(content, index_in_content)
    return TextCoords(line, col)
