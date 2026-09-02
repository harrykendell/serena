import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TypeVar

from serena.util.file_system import find_all_non_ignored_files
from solidlsp.ls_config import FilenameMatcher, LanguageServerId

T = TypeVar("T")

log = logging.getLogger(__name__)


def iter_subclasses(
    cls: type[T], recursive: bool = True, inclusion_predicate: Callable[[type[T]], bool] = lambda t: True
) -> Iterator[type[T]]:
    """Iterate over all subclasses of a class.

    :param cls: The class whose subclasses to iterate over.
    :param recursive: If True, also iterate over all subclasses of all subclasses.
    :param inclusion_predicate: a predicate function to decide whether to include a subclass in the result
    """
    for subclass in cls.__subclasses__():
        if inclusion_predicate(subclass):
            yield subclass
        if recursive:
            yield from iter_subclasses(subclass, recursive, inclusion_predicate)


_TEST_SOURCE_ROOT_NAMES = {"test", "tests", "spec", "specs"}
_TEST_FIXTURE_TREE_NAMES = {"examples", "fixtures", "repos", "resources", "samples", "test_data", "testdata"}


def _is_language_detection_fixture(repo_root: Path, file_path: Path) -> bool:
    """Returns whether ``file_path`` belongs to fixture data that should not define project languages."""
    try:
        parent_parts = file_path.resolve().relative_to(repo_root.resolve()).parts[:-1]
    except ValueError:
        return False

    for index, part in enumerate(parent_parts):
        if part in _TEST_SOURCE_ROOT_NAMES and any(descendant in _TEST_FIXTURE_TREE_NAMES for descendant in parent_parts[index + 1 :]):
            return True
    return False


def _preferred_language_server_for_filename(filename: str, matchers: dict[LanguageServerId, FilenameMatcher]) -> LanguageServerId | None:
    """Returns the first preferred language server matching ``filename``."""
    return next(
        (language for language, matcher in matchers.items() if matcher.is_relevant_filename(filename)),
        None,
    )


def detect_language_servers_for_files(file_paths: Iterable[str | Path], ls_ids: list[LanguageServerId]) -> list[LanguageServerId]:
    """Returns preferred language servers represented by ``file_paths`` in precedence order.

    :param file_paths: source-file paths whose basenames shall be inspected
    :param ls_ids: ordered candidate language servers
    :return: candidate language servers that own at least one provided file
    """
    matchers = {language: language.get_source_fn_matcher() for language in ls_ids}
    detected: set[LanguageServerId] = set()
    for file_path in file_paths:
        language = _preferred_language_server_for_filename(Path(file_path).name, matchers)
        if language is not None:
            detected.add(language)
    return [language for language in ls_ids if language in detected]


def compute_language_server_support_composition(
    repo_path: str, ls_ids: list[LanguageServerId] | None = None
) -> dict[LanguageServerId, float]:
    """
    Determines the source-language composition of a repository.

    Each recognised source file is attributed to one preferred language server. Test fixture/resource trees are excluded
    so embedded sample projects do not make their languages appear to be languages of the containing project.
    Percentages are relative to recognised project source files rather than the total file count.

    :param repo_path: path to the repository to analyze
    :param ls_ids: language servers to consider; if ``None``, use default non-experimental servers
    :return: mapping from language servers to percentages of recognised project source files
    """
    if ls_ids is None:
        ls_ids = list(LanguageServerId.iter_all(include_experimental=False))

    repo_root = Path(repo_path).resolve()
    all_files = [
        Path(file_path)
        for file_path in find_all_non_ignored_files(repo_path)
        if not _is_language_detection_fixture(repo_root, Path(file_path))
    ]
    if not all_files:
        return {}

    matchers = {language: language.get_source_fn_matcher() for language in ls_ids}
    language_file_counts: dict[LanguageServerId, int] = {}
    for file_path in all_files:
        language = _preferred_language_server_for_filename(file_path.name, matchers)
        if language is not None:
            language_file_counts[language] = language_file_counts.get(language, 0) + 1

    recognised_files = sum(language_file_counts.values())
    if recognised_files == 0:
        return {}

    return {language: round(count / recognised_files * 100, 2) for language, count in language_file_counts.items()}
