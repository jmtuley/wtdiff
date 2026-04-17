"""Tests for wtdiff."""
from unittest.mock import patch, MagicMock
import wtdiff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_run(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# filter_items
# ---------------------------------------------------------------------------

def test_filter_items_returns_all_when_query_is_empty():
    items = [{"path": "src/foo.py"}, {"path": "src/bar.py"}]

    result = wtdiff.filter_items(items, "", "path")

    assert result == items


def test_filter_items_returns_matching_items_by_substring():
    items = [{"path": "src/foo.py"}, {"path": "src/bar.py"}, {"path": "lib/foo.rb"}]

    result = wtdiff.filter_items(items, "foo", "path")

    assert result == [{"path": "src/foo.py"}, {"path": "lib/foo.rb"}]


def test_filter_items_is_case_insensitive():
    items = [{"path": "src/Foo.py"}, {"path": "src/bar.py"}]

    result = wtdiff.filter_items(items, "FOO", "path")

    assert result == [{"path": "src/Foo.py"}]


def test_filter_items_returns_empty_when_nothing_matches():
    items = [{"path": "src/foo.py"}, {"path": "src/bar.py"}]

    result = wtdiff.filter_items(items, "zzz", "path")

    assert result == []


def test_filter_items_returns_empty_when_items_list_is_empty():
    result = wtdiff.filter_items([], "foo", "path")

    assert result == []


# ---------------------------------------------------------------------------
# _bat_render_new_file
# ---------------------------------------------------------------------------

def test_bat_render_new_file_includes_new_file_notice():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("highlighted\n")):
        result = wtdiff._bat_render_new_file("/repo", "src/foo.py")

    assert "new file" in result.lower()


def test_bat_render_new_file_invokes_bat_with_color_and_no_paging():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("highlighted\n")) as mock_run:
        wtdiff._bat_render_new_file("/repo", "src/foo.py")

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "bat"
    assert "--color=always" in cmd
    assert "--paging=never" in cmd


# ---------------------------------------------------------------------------
# load_untracked
# ---------------------------------------------------------------------------

def test_load_untracked_empty_on_no_output():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("")):
        result = wtdiff.load_untracked("/repo")
    assert result == []


def test_load_untracked_returns_question_mark_entries():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("new_file.py\nanother.py")):
        result = wtdiff.load_untracked("/repo")
    assert result == [
        {"status": "?", "path": "new_file.py"},
        {"status": "?", "path": "another.py"},
    ]


# ---------------------------------------------------------------------------
# load_files
# ---------------------------------------------------------------------------

def test_load_files_empty_when_no_output():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run(""), _mock_run("")]):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == []


def test_load_files_parses_modified_file():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run("M\tsrc/foo.py"), _mock_run("")]):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "M", "path": "src/foo.py"}]


def test_load_files_parses_added_file():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run("A\tnew_file.py"), _mock_run("")]):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "A", "path": "new_file.py"}]


def test_load_files_parses_deleted_file():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run("D\told_file.py"), _mock_run("")]):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "D", "path": "old_file.py"}]


def test_load_files_parses_renamed_file():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run("R100\told.py\tnew.py"), _mock_run("")]):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [{"status": "R", "path": "new.py"}]


def test_load_files_parses_multiple_files():
    output = "M\tsrc/a.py\nA\tsrc/b.py\nD\tsrc/c.py"
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run(output), _mock_run("")]):
        result = wtdiff.load_files("/repo", "main", "branch")
    assert result == [
        {"status": "M", "path": "src/a.py"},
        {"status": "A", "path": "src/b.py"},
        {"status": "D", "path": "src/c.py"},
    ]


def test_load_files_includes_untracked_files():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run(""), _mock_run("new_file.py")]):
        result = wtdiff.load_files("/repo", "main", "dirty")
    assert result == [{"status": "?", "path": "new_file.py"}]


def test_load_files_untracked_appended_after_tracked():
    with patch("wtdiff.subprocess.run", side_effect=[
        _mock_run("M\tsrc/foo.py"),
        _mock_run("untracked.py"),
    ]):
        result = wtdiff.load_files("/repo", "main", "dirty")
    assert result == [
        {"status": "M", "path": "src/foo.py"},
        {"status": "?", "path": "untracked.py"},
    ]


def test_load_files_uses_branch_range_for_branch_mode():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run(""), _mock_run("")]) as mock_run:
        wtdiff.load_files("/repo", "main", "branch")
    args = mock_run.call_args_list[0][0][0]
    assert "main...HEAD" in args


def test_load_files_uses_head_for_dirty_mode():
    with patch("wtdiff.subprocess.run", side_effect=[_mock_run(""), _mock_run("")]) as mock_run:
        wtdiff.load_files("/repo", "main", "dirty")
    args = mock_run.call_args_list[0][0][0]
    assert "HEAD" in args
    assert "main...HEAD" not in args


# ---------------------------------------------------------------------------
# build_diff — tracked file
# ---------------------------------------------------------------------------

def test_build_diff_scopes_to_file_when_file_path_provided():
    with patch("wtdiff.subprocess.run", side_effect=[
        _mock_run(""),            # ls-files: file is tracked (not in untracked set)
        _mock_run("diff output"), # git diff with file args
    ]) as mock_run:
        wtdiff.build_diff("/repo", "main", "branch", "plain", file_path="src/foo.py")
    git_call_args = mock_run.call_args_list[1][0][0]
    assert "--" in git_call_args
    assert "src/foo.py" in git_call_args


def test_build_diff_does_not_scope_when_no_file_path():
    with patch("wtdiff.subprocess.run", side_effect=[
        _mock_run("diff output"), # git diff (full)
        _mock_run(""),            # load_untracked: no untracked files
    ]) as mock_run:
        wtdiff.build_diff("/repo", "main", "branch", "plain")
    git_call_args = mock_run.call_args_list[0][0][0]
    assert "src/foo.py" not in git_call_args
    assert "--" not in git_call_args


# ---------------------------------------------------------------------------
# build_diff — untracked files
# ---------------------------------------------------------------------------

def test_build_diff_uses_bat_for_single_untracked_when_available():
    with patch("wtdiff.shutil.which", return_value="/usr/bin/bat"), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("new_file.py"),    # ls-files
             _mock_run("+content\n"),     # _untracked_diff (empty-file check)
             _mock_run("bat output\n"),   # bat
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "plain", file_path="new_file.py")

    bat_call = mock_run.call_args_list[2][0][0]
    assert bat_call[0] == "bat"
    assert "bat output" in result


def test_build_diff_skips_bat_for_single_untracked_when_unavailable():
    with patch("wtdiff.shutil.which", return_value=None), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("new_file.py"),    # ls-files
             _mock_run("+content\n"),     # _untracked_diff (empty-file check)
             _mock_run("+content\n"),     # _untracked_diff_color
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "plain", file_path="new_file.py")

    calls = [c[0][0] for c in mock_run.call_args_list]
    assert not any(c[0] == "bat" for c in calls)
    assert "+content" in result

def test_build_diff_uses_no_index_for_untracked_file():
    patch_content = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1 @@\n+hello\n"
    with patch("wtdiff.subprocess.run", side_effect=[
        _mock_run("new_file.py"),  # ls-files: file is untracked
        _mock_run(patch_content),  # _untracked_diff (plain)
        _mock_run(patch_content),  # _untracked_diff_color
    ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "plain", file_path="new_file.py")
    no_index_call = mock_run.call_args_list[1][0][0]
    assert "--no-index" in no_index_call
    assert "/dev/null" in no_index_call
    assert result != "(no uncommitted changes)"


def test_untracked_diff_uses_dash_c_and_relative_path():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("+line\n")) as mock_run:
        wtdiff._untracked_diff("/repo", "src/new_file.py")

    cmd = mock_run.call_args[0][0]
    assert "-C" in cmd
    assert "/repo" in cmd
    assert "src/new_file.py" in cmd
    assert "/repo/src/new_file.py" not in cmd


def test_untracked_diff_color_uses_dash_c_and_relative_path():
    with patch("wtdiff.subprocess.run", return_value=_mock_run("+line\n")) as mock_run:
        wtdiff._untracked_diff_color("/repo", "src/new_file.py")

    cmd = mock_run.call_args[0][0]
    assert "-C" in cmd
    assert "/repo" in cmd
    assert "src/new_file.py" in cmd
    assert "/repo/src/new_file.py" not in cmd


def test_build_diff_appends_untracked_in_full_diff():
    with patch("wtdiff.subprocess.run", side_effect=[
        _mock_run("tracked diff output"),  # git diff (full, tracked)
        _mock_run("untracked.py"),         # load_untracked
        _mock_run("+untracked content\n"), # _untracked_diff_color for untracked.py
    ]):
        result = wtdiff.build_diff("/repo", "main", "dirty", "plain")
    assert "tracked diff output" in result
    assert "+untracked content" in result


def test_build_diff_difft_single_untracked_uses_external_diff():
    difft_output = "difft highlighted output\n"
    with patch("wtdiff.shutil.which", return_value=None), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("new_file.py"),  # ls-files: file is untracked
             _mock_run("+content\n"),   # _untracked_diff plain (empty-file check)
             _mock_run(difft_output),   # difft invocation via GIT_EXTERNAL_DIFF
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "difft", file_path="new_file.py")

    difft_call = mock_run.call_args_list[2]
    assert difft_call[1]["env"]["GIT_EXTERNAL_DIFF"] == "difft"
    assert result == difft_output


def test_build_diff_full_worktree_uses_bat_for_untracked_when_available():
    with patch("wtdiff.shutil.which", return_value="/usr/bin/bat"), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("tracked diff\n"),  # git diff (tracked)
             _mock_run("untracked.py"),    # load_untracked
             _mock_run("bat output\n"),    # bat for untracked.py
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "plain")

    bat_call = mock_run.call_args_list[2][0][0]
    assert bat_call[0] == "bat"
    assert "tracked diff" in result
    assert "bat output" in result


def test_build_diff_full_worktree_bat_processes_tracked_through_tool_first():
    with patch("wtdiff.shutil.which", return_value="/usr/bin/bat"), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("tracked diff\n"),  # git diff (tracked)
             _mock_run("delta output\n"),  # delta processes tracked diff
             _mock_run("untracked.py"),    # load_untracked
             _mock_run("bat output\n"),    # bat for untracked.py
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "delta")

    delta_call = mock_run.call_args_list[1][0][0]
    assert delta_call == ["delta"]
    assert "delta output" in result
    assert "bat output" in result


def test_build_diff_difft_full_worktree_uses_bat_for_untracked_when_available():
    with patch("wtdiff.shutil.which", return_value="/usr/bin/bat"), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("difft tracked\n"),  # difft git diff
             _mock_run("untracked.py"),     # load_untracked
             _mock_run("bat output\n"),     # bat for untracked.py
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "difft")

    bat_call = mock_run.call_args_list[2][0][0]
    assert bat_call[0] == "bat"
    assert "bat output" in result


def test_build_diff_difft_full_worktree_uses_external_diff_for_untracked():
    with patch("wtdiff.shutil.which", return_value=None), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("tracked diff\n"),  # git diff (tracked, via difft)
             _mock_run("untracked.py"),    # load_untracked
             _mock_run("difft new\n"),     # difft invocation for untracked.py
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "difft")

    difft_call = mock_run.call_args_list[2]
    assert difft_call[1]["env"]["GIT_EXTERNAL_DIFF"] == "difft"
    assert "difft new" in result


def test_build_diff_difft_single_untracked_falls_back_when_difft_empty():
    with patch("wtdiff.shutil.which", return_value=None), \
         patch("wtdiff.subprocess.run", side_effect=[
             _mock_run("new_file.py"),  # ls-files: file is untracked
             _mock_run("+content\n"),   # _untracked_diff plain (empty-file check)
             _mock_run(""),             # difft returns nothing
             _mock_run("+content\n"),   # _untracked_diff fallback
         ]) as mock_run:
        result = wtdiff.build_diff("/repo", "main", "dirty", "difft", file_path="new_file.py")

    assert "+content" in result


def test_build_diff_untracked_only_shows_no_empty_message():
    patch_content = "+new content\n"
    with patch("wtdiff.subprocess.run", side_effect=[
        _mock_run(""),             # git diff: no tracked changes
        _mock_run("new_file.py"),  # load_untracked
        _mock_run(patch_content),  # _untracked_diff_color
    ]):
        result = wtdiff.build_diff("/repo", "main", "dirty", "plain")
    assert result != "(no uncommitted changes)"
    assert "+new content" in result
