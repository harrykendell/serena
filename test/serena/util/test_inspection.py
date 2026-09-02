from pathlib import Path

from serena.util.inspection import compute_language_server_support_composition
from solidlsp.ls_config import LanguageServerId


def _touch(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("content", encoding="utf-8")


class TestComputeLanguageServerSupportComposition:
    def test_single_language_repo(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py", "b.py", "c.py")
        composition = compute_language_server_support_composition(str(tmp_path))
        assert composition[LanguageServerId.PYTHON] == 100.0

    def test_unrecognised_files_do_not_dilute_percentages(self, tmp_path: Path) -> None:
        # 2 source files vs many files that belong to no supported language
        _touch(tmp_path, "main.py", "util.py")
        _touch(tmp_path, *[f"note_{i}.txt" for i in range(20)])
        _touch(tmp_path, "logo.png", "LICENSE", "data.csv")

        composition = compute_language_server_support_composition(str(tmp_path))

        # previously: 2 / 25 files = 8% — now the denominator is recognised source files only
        assert composition[LanguageServerId.PYTHON] == 100.0

    def test_mixed_language_percentages_relative_to_recognised_files(self, tmp_path: Path) -> None:
        _touch(tmp_path, "a.py", "b.py", "c.py", "d.go")
        _touch(tmp_path, *[f"asset_{i}.dat" for i in range(50)])

        composition = compute_language_server_support_composition(str(tmp_path))

        assert composition[LanguageServerId.PYTHON] == 75.0
        assert composition[LanguageServerId.GO] == 25.0

    def test_overlapping_language_servers_assign_each_file_once(self, tmp_path: Path) -> None:
        _touch(tmp_path, "app.ts", "component.svelte")

        composition = compute_language_server_support_composition(
            str(tmp_path),
            [LanguageServerId.TYPESCRIPT, LanguageServerId.SVELTE, LanguageServerId.VUE],
        )

        assert composition == {
            LanguageServerId.TYPESCRIPT: 50.0,
            LanguageServerId.SVELTE: 50.0,
        }

    def test_embedded_test_fixture_repositories_do_not_define_project_languages(self, tmp_path: Path) -> None:
        _touch(tmp_path, "main.py")
        fixture_root = tmp_path / "test" / "resources" / "repos" / "rust"
        fixture_root.mkdir(parents=True)
        _touch(fixture_root, "fixture.rs")

        assert compute_language_server_support_composition(str(tmp_path)) == {
            LanguageServerId.PYTHON: 100.0,
        }

    def test_repo_without_recognised_files(self, tmp_path: Path) -> None:
        _touch(tmp_path, "readme.txt", "logo.png")
        assert compute_language_server_support_composition(str(tmp_path)) == {}

    def test_empty_repo(self, tmp_path: Path) -> None:
        assert compute_language_server_support_composition(str(tmp_path)) == {}
