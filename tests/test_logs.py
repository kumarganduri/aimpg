from aimpg.logs import bash_paths, commit_cwd, iter_log_files, parse_logs

from conftest import assistant, bash_call, tool_result

T0 = "2026-09-30T19:41:41.000Z"
T1 = "2026-09-30T19:41:44.000Z"


def test_dedup_keeps_max_output_row(write_log):
    # Claude Code repeats usage on every content-block row; a partial
    # streaming row can carry a smaller output count.
    path = write_log([
        assistant("req_a", T0, out=5),
        assistant("req_a", T0, out=50),
        assistant("req_a", T0, out=50),
    ])
    result = parse_logs([path])
    assert len(result.requests) == 1
    assert result.requests[0].usage.output == 50
    assert result.stats["duplicate_rows"] == 2
    assert result.stats["usage_rows"] == 3


def test_dedup_across_files_for_resumed_sessions(write_log):
    a = write_log([assistant("req_a", T0)], name="one.jsonl")
    b = write_log([assistant("req_a", T0)], name="two.jsonl")
    assert len(parse_logs([a, b]).requests) == 1


def test_synthetic_model_dropped_and_not_counted_as_coverage_loss(write_log):
    path = write_log([assistant("req_a", T0), assistant("req_b", T0, model="<synthetic>")])
    result = parse_logs([path])
    assert [r.id for r in result.requests] == ["req_a"]
    assert result.stats["skipped_synthetic"] == 1
    assert result.coverage == 1.0


def test_missing_request_id_falls_back_to_session_and_message_id(write_log):
    path = write_log([
        assistant(None, T0, message_id="msg_9", out=3),
        assistant(None, T0, message_id="msg_9", out=7),
    ])
    result = parse_logs([path])
    assert [r.id for r in result.requests] == ["s1:msg_9"]
    assert result.requests[0].usage.output == 7


def test_usage_maps_to_buckets_and_ctx_len(write_log):
    path = write_log([assistant("req_a", T0, fresh=2, write=48439, read=52392, out=294)])
    usage = parse_logs([path]).requests[0].usage
    assert (usage.fresh_in, usage.cache_write, usage.cache_read, usage.output) == (2, 48439, 52392, 294)
    assert usage.ctx_len == 2 + 48439 + 52392


def test_corrupt_and_bad_shape_rows_are_counted_not_fatal(write_log):
    bad_usage = assistant("req_b", T0)
    bad_usage["message"]["usage"]["output_tokens"] = "lots"
    no_ts = assistant("req_c", "not-a-time")
    path = write_log(
        [assistant("req_a", T0), bad_usage, no_ts, {"type": "mystery", "x": 1}, ["not", "a", "dict", "usage"]],
        raw_lines=['{"type": "assistant", "message": {"usage": {trunc'],
    )
    result = parse_logs([path])
    assert [r.id for r in result.requests] == ["req_a"]
    assert result.stats["corrupt_rows"] == 2  # truncated line + non-dict row
    assert result.stats["skipped_bad_shape"] == 2
    assert result.coverage == 1 / 3


def test_sidechain_rows_keep_parent_session(write_log):
    path = write_log([assistant("req_a", T0, session="parent", sidechain=True)])
    request = parse_logs([path]).requests[0]
    assert request.session_id == "parent"
    assert request.is_sidechain


def test_row_cwd_is_used_not_log_folder(write_log):
    path = write_log([assistant("req_a", T0, cwd="/Users/me/other-project")])
    assert parse_logs([path]).requests[0].cwd == "/Users/me/other-project"


def test_commit_call_interval_from_tool_use_to_result(write_log):
    path = write_log([
        bash_call("tu_1", 'git add -A && git commit -m "feat: x"', T0),
        tool_result("tu_1", T1),
    ])
    (call,) = parse_logs([path]).commit_calls
    assert call.cwd == "/repo"
    assert call.end - call.start == 3.0


def test_commit_call_without_result_has_open_end(write_log):
    path = write_log([bash_call("tu_1", "git commit -m x", T0)])
    assert parse_logs([path]).commit_calls[0].end is None


def test_non_commit_bash_is_ignored(write_log):
    path = write_log([bash_call("tu_1", "git log --oneline | grep commit", T0)])
    assert parse_logs([path]).commit_calls == []


def test_edit_tools_record_files_touched(write_log):
    path = write_log([
        assistant("req_a", T0, content=[
            {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "/repo/a.py"}},
            {"type": "tool_use", "id": "e2", "name": "NotebookEdit", "input": {"notebook_path": "/repo/n.ipynb"}},
        ]),
    ])
    assert parse_logs([path]).files_touched["s1"] == {("/repo", "/repo/a.py"), ("/repo", "/repo/n.ipynb")}


def test_versions_are_counted(write_log):
    path = write_log([assistant("req_a", T0, version="2.1.280"), assistant("req_b", T0, version="2.1.300")])
    assert parse_logs([path]).versions == {"2.1.280": 1, "2.1.300": 1}


def test_missing_root_yields_no_files(tmp_path):
    assert list(iter_log_files(tmp_path / "nope")) == []


class TestCommitCwd:
    def test_plain(self):
        assert commit_cwd('git commit -m "x"', "/repo") == "/repo"

    def test_cd_prefix(self):
        assert commit_cwd("cd ~/other-project && git commit -m x", "/elsewhere").endswith("/other-project")

    def test_relative_cd(self):
        assert commit_cwd("cd sub && git commit -m x", "/repo") == "/repo/sub"

    def test_dash_c(self):
        assert commit_cwd("git -C /other commit -m x", "/repo") == "/other"

    def test_not_a_commit(self):
        assert commit_cwd("git status && git log", "/repo") is None

    def test_commit_text_in_pipe_is_not_a_commit(self):
        assert commit_cwd("git log | grep commit", "/repo") is None

    def test_unbalanced_quotes_do_not_crash(self):
        assert commit_cwd("git commit -m \"unterminated", "/repo") == "/repo"


class TestBashPaths:
    def test_sed_in_place(self):
        assert bash_paths("sed -i '' 's/a/b/' src/x.py src/y.py") == {"src/x.py", "src/y.py"}

    def test_sed_with_expression_flag(self):
        assert bash_paths("sed -i -e 's/a/b/' x.py") == {"x.py"}

    def test_sed_without_in_place_writes_nothing(self):
        assert bash_paths("sed 's/a/b/' x.py") == set()

    def test_perl_pie(self):
        assert bash_paths("perl -pi -e 's/a/b/' x.py") == {"x.py"}

    def test_redirects(self):
        assert bash_paths("echo hi > notes.md && cat a >> log.txt") == {"notes.md", "log.txt"}

    def test_heredoc_redirect(self):
        assert bash_paths("cat > docs/plan.md <<'EOF'\nhello\nEOF") == {"docs/plan.md"}

    def test_dev_null_and_stderr_ignored(self):
        assert bash_paths("make 2>/dev/null > /dev/null") == set()

    def test_mv_cp_rm_touch_tee(self):
        assert bash_paths("mv a.py b.py && rm -f c.py && echo x | tee d.txt") == {"a.py", "b.py", "c.py", "d.txt"}

    def test_variables_skipped(self):
        assert bash_paths('echo x > "$OUT"') == set()


def test_bash_edits_recorded_as_files_touched(write_log):
    path = write_log([bash_call("tu_1", "sed -i '' 's/a/b/' a.py", T0)])
    assert parse_logs([path]).files_touched["s1"] == {("/repo", "a.py")}
