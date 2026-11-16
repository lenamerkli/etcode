import os
import threading
import time

import pytest

from etcode_desktop.core.tools import CancelToken, ToolContext, execute_tool
from etcode_desktop.core.tools.bash import format_result


def bash(tmp_path, command, cancel=None, **arguments):
    context = ToolContext(cancel=cancel or CancelToken(), home=tmp_path)
    parts = execute_tool("bash", {"command": command, "directory": str(tmp_path), **arguments}, context)
    assert len(parts) == 1 and parts[0]["type"] == "text"
    return parts[0]["text"]


def kill(pid_file):
    """End a process that a test left behind: exactly that one, never by name."""
    try:
        os.kill(int(pid_file.read_text()), 9)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not os.path.exists(f"/proc/{pid}/status") or "Z (zombie)" not in open(f"/proc/{pid}/status").read()


# --- the format ------------------------------------------------------------------------------------------------------------------

def test_the_exact_result_text(tmp_path):
    assert bash(tmp_path, "echo out; echo err >&2; exit 3") == "<returncode>3</returncode>\n<stderr>err\n</stderr>\n<stdout>out\n</stdout>"


def test_a_silent_command(tmp_path):
    assert bash(tmp_path, "true") == "<returncode>0</returncode>\n<stderr></stderr>\n<stdout></stdout>"


def test_the_format_helper():
    assert format_result(0, "e", "o") == "<returncode>0</returncode>\n<stderr>e</stderr>\n<stdout>o</stdout>"


def test_stdout_and_stderr_are_kept_apart(tmp_path):
    text = bash(tmp_path, "for i in 1 2 3; do echo o$i; echo e$i >&2; done")
    assert "<stderr>e1\ne2\ne3\n</stderr>" in text and "<stdout>o1\no2\no3\n</stdout>" in text


def test_multi_line_unicode_and_missing_final_newline(tmp_path):
    assert bash(tmp_path, "printf 'héllo\\nwörld \\xe2\\x82\\xac'") == (
        "<returncode>0</returncode>\n<stderr></stderr>\n<stdout>héllo\nwörld €</stdout>")


def test_invalid_utf8_is_replaced(tmp_path):
    assert "a\ufffdb" in bash(tmp_path, "printf 'a\\xffb'")


def test_the_exit_code_is_reported(tmp_path):
    for code in (0, 1, 2, 42, 255):
        assert bash(tmp_path, f"exit {code}").startswith(f"<returncode>{code}</returncode>")


def test_the_command_runs_in_the_directory_and_is_a_real_shell(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    text = bash(tmp_path, "pwd; ls; echo $((6*7)); echo a | tr a b; cd / && pwd")
    assert f"{tmp_path}\nmarker.txt\n42\nb\n/\n" in text


def test_stdin_is_closed(tmp_path):
    started = time.monotonic()
    assert bash(tmp_path, "cat; echo done").endswith("<stdout>done\n</stdout>")  # cat would wait for a terminal
    assert time.monotonic() - started < 5


# --- directory, venv, environment --------------------------------------------------------------------------------------------------

def test_a_missing_directory_gives_return_code_minus_one(tmp_path):
    text = bash(tmp_path, "echo hi", directory=str(tmp_path / "nope"))
    assert text.startswith("<returncode>-1</returncode>\n<stderr>cannot start the command: the directory") and text.endswith("<stdout></stdout>")


def test_a_relative_directory_is_relative_to_home(tmp_path):
    (tmp_path / "sub").mkdir()
    assert f"{tmp_path / 'sub'}\n" in bash(tmp_path, "pwd", directory="sub")


def test_the_default_directory_falls_back_to_home_when_it_does_not_exist(tmp_path):
    context = ToolContext(home=tmp_path)
    parts = execute_tool("bash", {"command": "pwd"}, context)  # no directory: the default is /home/agent
    assert str(tmp_path) in parts[0]["text"] or os.path.isdir("/home/agent")


def test_venv_sets_the_environment(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    text = bash(tmp_path, 'echo "$VIRTUAL_ENV"; echo "$PATH" | cut -d: -f1; echo "[$PYTHONHOME]"', venv=str(venv))
    assert f"{venv}\n{venv}/bin\n[]" in text


def test_pythonhome_is_removed_with_a_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/somewhere")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    assert "[]" in bash(tmp_path, 'echo "[$PYTHONHOME]"', venv=str(venv))
    assert "[/somewhere]" in bash(tmp_path, 'echo "[$PYTHONHOME]"')  # without a venv it is left alone


def test_a_venv_that_does_not_exist_is_an_error(tmp_path):
    result = execute_tool("bash", {"command": "ls", "directory": str(tmp_path), "venv": str(tmp_path / "nope")}, ToolContext(home=tmp_path))
    assert result[0]["text"].startswith("Error: the virtual environment") and "does not exist" in result[0]["text"]


def test_the_api_token_is_not_passed_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ETCODE_API_TOKEN", "tok-secret-1234")
    monkeypatch.setenv("SOMETHING_ELSE", "visible")
    text = bash(tmp_path, 'echo "[$ETCODE_API_TOKEN] [$SOMETHING_ELSE]"')
    assert "[] [visible]" in text and "tok-secret-1234" not in text


# --- limits ----------------------------------------------------------------------------------------------------------------------------

def test_a_command_that_runs_too_long_is_killed(tmp_path):
    started = time.monotonic()
    text = bash(tmp_path, "echo before; sleep 30; echo AFTERWORD", timeout=1)
    assert time.monotonic() - started < 8
    assert "<stdout>before\n</stdout>" in text and "AFTERWORD" not in text
    assert "Command timed out after 1 seconds" in text
    code = int(text.split("<returncode>")[1].split("</returncode>")[0])
    assert code < 0  # ended by a signal


def test_the_timeout_note_goes_on_a_new_line(tmp_path):
    text = bash(tmp_path, "printf 'partial'; sleep 30", timeout=1)
    assert "<stderr></stderr>" not in text and "Command timed out" in text
    text = bash(tmp_path, "printf 'no newline' >&2; sleep 30", timeout=1)
    assert "no newline\nCommand timed out after 1 seconds</stderr>" in text


def test_a_timeout_kills_what_the_command_started(tmp_path):
    pid_file = tmp_path / "pid"
    bash(tmp_path, f"sleep 60 & echo $! > {pid_file}; sleep 60", timeout=1)
    pid = int(pid_file.read_text())
    time.sleep(0.3)
    assert not alive(pid)


def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path):
    started = time.monotonic()
    text = bash(tmp_path, "trap '' TERM; echo ready; sleep 60", timeout=1)
    assert time.monotonic() - started < 10 and "Command timed out" in text and "<returncode>-9</returncode>" in text


def test_a_huge_output_is_cut_and_does_not_fill_memory(tmp_path):
    started = time.monotonic()
    text = bash(tmp_path, "yes 'some long line of output that repeats' | head -c 60000000", max_chars=1000)
    assert time.monotonic() - started < 20
    assert text.startswith("<returncode>0</returncode>\n<stderr></stderr>\n<stdout>some long line")
    body, note = text.rsplit("\n[output truncated, ", 1)
    assert len(body) == 1000 and note.endswith(" more characters]")
    assert int(note.split()[0]) > 50_000_000


def test_the_whole_response_is_cut_not_just_stdout(tmp_path):
    text = bash(tmp_path, "echo " + "e" * 500 + " >&2; echo " + "o" * 500, max_chars=300)
    assert text.startswith("<returncode>0</returncode>\n<stderr>eeee") and "<stdout>" not in text  # stderr comes first and fills it
    assert text.endswith(" more characters]")


def test_output_that_fits_is_not_marked(tmp_path):
    text = bash(tmp_path, "echo hello", max_chars=1000)
    assert "truncated" not in text
    exact = format_result(0, "", "hello\n")
    assert bash(tmp_path, "echo hello", max_chars=len(exact)) == exact  # exactly full: no note
    assert "truncated, 1 more characters" in bash(tmp_path, "echo hello", max_chars=len(exact) - 1)


def test_a_command_that_floods_stderr_and_stdout_at_once(tmp_path):
    text = bash(tmp_path, "yes err | head -c 20000000 >&2 & yes out | head -c 20000000; wait", max_chars=500)
    assert text.startswith("<returncode>0</returncode>") and "[output truncated," in text


# --- processes that outlive the command -----------------------------------------------------------------------------------------------

def test_a_background_process_does_not_keep_the_call_waiting(tmp_path):
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    text = bash(tmp_path, f"sleep 30 & echo $! > {pid_file}; echo started", timeout=20)
    assert time.monotonic() - started < 5, "the call waited for the background process"
    assert "<stdout>started\n</stdout>" in text and "<returncode>0</returncode>" in text
    kill(pid_file)


def test_a_background_process_is_left_running(tmp_path):
    pid_file = tmp_path / "pid"
    bash(tmp_path, f"nohup sleep 30 > /dev/null 2>&1 & echo $! > {pid_file}")
    pid = int(pid_file.read_text())
    try:
        assert alive(pid)  # a server that the agent started on purpose must survive the call
    finally:
        os.kill(pid, 9)


def test_a_background_process_that_keeps_printing_does_not_hold_the_call(tmp_path):
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    text = bash(tmp_path, f"(while true; do echo spam; sleep 0.01; done) & echo $! > {pid_file}; echo main", timeout=20)
    assert time.monotonic() - started < 5 and "main" in text
    kill(pid_file)


# --- cancelling ------------------------------------------------------------------------------------------------------------------------

def test_cancel_kills_the_command_and_its_children_quickly(tmp_path):
    pid_file = tmp_path / "pid"
    cancel = CancelToken()
    results = []
    thread = threading.Thread(target=lambda: results.append(bash(tmp_path, f"sleep 60 & echo $! > {pid_file}; sleep 60", cancel=cancel)))
    started = time.monotonic()
    thread.start()
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    cancel.cancel()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 8
    assert "Command was cancelled" in results[0] and "timed out" not in results[0]
    time.sleep(0.3)
    assert not alive(int(pid_file.read_text()))


def test_a_token_that_is_already_cancelled_stops_the_command_at_once(tmp_path):
    cancel = CancelToken()
    cancel.cancel()
    started = time.monotonic()
    text = bash(tmp_path, "sleep 30", cancel=cancel)
    assert time.monotonic() - started < 6 and "Command was cancelled" in text


def test_many_commands_at_once(tmp_path):
    results = []

    def run(i):
        results.append(bash(tmp_path, f"sleep 0.3; echo n{i}"))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(12)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert time.monotonic() - started < 5  # side by side, not one after the other
    assert sorted(r.split("<stdout>")[1].split("</stdout>")[0].strip() for r in results) == sorted(f"n{i}" for i in range(12))


# --- bad calls ---------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("arguments, message", [
    ({"timeout": 0}, "timeout must be at least 1 second"),
    ({"timeout": -5}, "timeout must be at least 1 second"),
    ({"max_chars": 0}, "max_chars must be at least 1"),
    ({"timeout": "soon"}, "'timeout' must be an integer"),
])
def test_bad_arguments_are_errors_for_the_model(tmp_path, arguments, message):
    result = execute_tool("bash", {"command": "echo hi", "directory": str(tmp_path), **arguments}, ToolContext(home=tmp_path))
    assert result[0]["text"].startswith("Error: invalid call of bash:") and message in result[0]["text"]


def test_a_missing_command(tmp_path):
    result = execute_tool("bash", {"directory": str(tmp_path)}, ToolContext(home=tmp_path))
    assert result[0]["text"] == "Error: invalid call of bash: missing required argument 'command'"
