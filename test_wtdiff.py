"""Tests for wtdiff."""
from unittest.mock import patch, MagicMock
import wtdiff


# ---------------------------------------------------------------------------
# load_files
# ---------------------------------------------------------------------------

def _mock_run(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_load_files_empty_when_no_output():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("")) as mock_run:
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == []


def test_load_files_parses_modified_file():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("M\tsrc/foo.py")):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "M", "path": "src/foo.py"}]


def test_load_files_parses_added_file():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("A\tnew_file.py")):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "A", "path": "new_file.py"}]


def test_load_files_parses_deleted_file():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("D\told_file.py")):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "D", "path": "old_file.py"}]


def test_load_files_parses_renamed_file():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("R100\told.py\tnew.py")):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "R", "path": "new.py"}]


def test_load_files_parses_multiple_files():
    output = "M\tsrc/a.py\nA\tsrc/b.py\nD\tsrc/c.py"
    with patch("wtdiff.subprocess.run", return_value=_mock_run(output)):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [
        {"status": "M", "path": "src/a.py"},
        {"status": "A", "path": "src/b.py"},
        {"status": "D", "path": "src/c.py"},
    ]


def test_load_files_uses_branch_range_for_branch_mode():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("")) as mock_run:
        wtdiff.load_files("/repo", "main", "branch")
    args = mock_run.call_args[0][0]
    assert "main...HEAD" in args


def test_load_files_uses_head_for_dirty_mode():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("")) as mock_run:
        wtdiff.load_files("/repo", "main", "dirty")
    args = mock_run.call_args[0][0]
    assert "HEAD" in args
    assert "main...HEAD" not in args


# ---------------------------------------------------------------------------
# build_diff file_path
# ---------------------------------------------------------------------------

def test_build_diff_scopes_to_file_when_file_path_provided():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("diff output")) as mock_run:
        wtdiff.build_diff("/repo", "main", "branch", "plain", file_path="src/foo.py")
    git_call_args = mock_run.call_args_list[0][0][0]
    assert "--" in git_call_args
    assert "src/foo.py" in git_call_args


def test_build_diff_does_not_scope_when_no_file_path():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("diff output")) as mock_run:
        wtdiff.build_diff("/repo", "main", "branch", "plain")
    git_call_args = mock_run.call_args_list[0][0][0]
    assert "src/foo.py" not in git_call_args
    assert "--" not in git_call_args
