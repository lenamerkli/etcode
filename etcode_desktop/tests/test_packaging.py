"""The files that install the app into an etcode VM: launcher, .desktop entries, icon (see PLAN.md, milestone 9)."""
import configparser
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
LAUNCHER = PACKAGING / "etcode-desktop"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the launcher is a POSIX shell script")


def read_desktop_entry(path: Path) -> dict[str, str]:
    parser = configparser.RawConfigParser(strict=True)
    parser.optionxform = str  # keys are case sensitive
    parser.read(path, encoding="utf-8")
    assert parser.sections() == ["Desktop Entry"]
    return dict(parser["Desktop Entry"])


def run_launcher(tmp_path, *args, env_extra=None, timeout=30):
    """Run the launcher against a fake app: a 'python' that only prints what it was started with."""
    fake_python = tmp_path / "python"
    fake_python.write_text('#!/bin/sh\necho "python=$0"\necho "args=$*"\necho "pythonpath=$PYTHONPATH"\n'
                           'echo "qpa=$QT_QPA_PLATFORM"\n')
    fake_python.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "ETCODE_PYTHON": str(fake_python), "ETCODE_DESKTOP_HOME": str(tmp_path / "app")}
    env.update(env_extra or {})
    started = time.monotonic()
    result = subprocess.run(["sh", str(LAUNCHER), *args], env=env, capture_output=True, text=True, timeout=timeout)
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return result, lines, time.monotonic() - started


# --- launcher -------------------------------------------------------------------------------

def test_launcher_is_executable_and_valid_shell():
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR
    assert LAUNCHER.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["sh", "-n", str(LAUNCHER)], capture_output=True).returncode == 0


def test_launcher_runs_the_app_with_the_venv_python_and_xcb(tmp_path):
    result, seen, _ = run_launcher(tmp_path, "--maximized", "-platform", "offscreen")
    assert result.returncode == 0, result.stderr
    assert seen["python"] == str(tmp_path / "python")
    assert seen["args"] == "-m etcode_desktop --maximized -platform offscreen"
    assert seen["pythonpath"] == str(tmp_path / "app")
    assert seen["qpa"] == "xcb"


def test_launcher_keeps_an_existing_platform_and_pythonpath(tmp_path):
    _, seen, _ = run_launcher(tmp_path, env_extra={"QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": "/x"})
    assert seen["qpa"] == "offscreen"
    assert seen["pythonpath"] == f"{tmp_path / 'app'}:/x"


def test_launcher_names_the_problem_when_the_app_is_not_installed(tmp_path):
    result, _, _ = run_launcher(tmp_path, env_extra={"ETCODE_PYTHON": str(tmp_path / "missing")})
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_launcher_starts_the_real_app(tmp_path):
    result = subprocess.run(
        ["sh", str(LAUNCHER), "--version"],
        env={"PATH": os.environ["PATH"], "ETCODE_PYTHON": sys.executable, "ETCODE_DESKTOP_HOME": str(ROOT),
             "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("etcode-desktop ")


# --- --wait-for-provisioning --------------------------------------------------------------------------

def wait_env(tmp_path, wait="10", run_dir=True):
    run = tmp_path / "run-cloud-init"
    if run_dir:
        run.mkdir(exist_ok=True)
    return {"ETCODE_PROVISIONED_FILE": str(tmp_path / "desktop.json"), "ETCODE_CLOUD_INIT_RUN_DIR": str(run),
            "ETCODE_PROVISIONING_WAIT": wait}


def test_no_waiting_without_the_flag(tmp_path):
    _, seen, took = run_launcher(tmp_path, env_extra=wait_env(tmp_path))
    assert took < 5 and seen["args"] == "-m etcode_desktop"


def test_the_flag_is_not_passed_on_to_the_app(tmp_path):
    _, seen, _ = run_launcher(tmp_path, "--wait-for-provisioning", "--maximized", env_extra=wait_env(tmp_path, run_dir=False))
    assert seen["args"] == "-m etcode_desktop --maximized"


def test_no_waiting_when_there_is_no_cloud_init(tmp_path):
    _, _, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=wait_env(tmp_path, run_dir=False))
    assert took < 3


def test_no_waiting_when_the_file_is_already_there(tmp_path):
    (tmp_path / "desktop.json").write_text("{}")
    _, _, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=wait_env(tmp_path))
    assert took < 3


def test_no_waiting_when_cloud_init_has_finished(tmp_path):
    env = wait_env(tmp_path)
    (Path(env["ETCODE_CLOUD_INIT_RUN_DIR"]) / "result.json").write_text("{}")
    _, _, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=env)
    assert took < 3


def test_waits_until_the_file_appears(tmp_path):
    env = wait_env(tmp_path, wait="30")
    threading.Timer(2.0, lambda: (tmp_path / "desktop.json").write_text("{}")).start()
    result, seen, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=env)
    assert result.returncode == 0 and "args" in seen
    assert 1.5 < took < 12


def test_waits_until_cloud_init_finishes(tmp_path):
    env = wait_env(tmp_path, wait="30")
    done = Path(env["ETCODE_CLOUD_INIT_RUN_DIR"]) / "result.json"
    threading.Timer(2.0, lambda: done.write_text("{}")).start()
    _, _, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=env)
    assert 1.5 < took < 12


def test_gives_up_after_the_limit_and_still_starts(tmp_path):
    result, seen, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=wait_env(tmp_path, wait="2"))
    assert result.returncode == 0 and "args" in seen
    assert 1.5 < took < 10


def test_a_bad_limit_falls_back_to_the_default(tmp_path):
    (tmp_path / "desktop.json").write_text("{}")  # so that the default of 60 s is not waited for
    result, _, took = run_launcher(tmp_path, "--wait-for-provisioning", env_extra=wait_env(tmp_path, wait="soon"))
    assert result.returncode == 0 and took < 3


# --- .desktop entries and the icon ----------------------------------------------------------------------

def test_menu_entry():
    entry = read_desktop_entry(PACKAGING / "etcode-desktop.desktop")
    assert entry["Type"] == "Application"
    assert entry["Exec"] == "etcode-desktop"
    assert entry["Icon"] == "etcode-desktop"
    assert entry["Terminal"] == "false"
    assert entry["Name"]


def test_autostart_entry():
    entry = read_desktop_entry(PACKAGING / "etcode-desktop-autostart.desktop")
    assert entry["Type"] == "Application"
    assert entry["Exec"].split() == ["etcode-desktop", "--wait-for-provisioning", "--maximized"]
    assert entry["X-GNOME-Autostart-Delay"] == "2"  # the panel is up first
    assert entry.get("Hidden", "false") != "true"  # a hidden autostart entry would not start anything
    assert "OnlyShowIn" not in entry  # autostart in every session type of the VM


def test_exec_commands_accept_the_options_the_app_has():
    from etcode_desktop.__main__ import parse_args

    args, leftovers = parse_args(["--maximized"])
    assert args.maximized and leftovers == []


def test_icon_is_valid_svg():
    root = ET.parse(PACKAGING / "etcode-desktop.svg").getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox") == "0 0 128 128"


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-utils not installed")
@pytest.mark.parametrize("name", ["etcode-desktop.desktop", "etcode-desktop-autostart.desktop"])
def test_desktop_file_validates(name):
    result = subprocess.run(["desktop-file-validate", str(PACKAGING / name)], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == "", result.stdout + result.stderr
