"""Tests for the indefinite pause (files and subprocesses only; no database, network or Claude)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import pause

ROOT = Path(__file__).resolve().parent.parent
# If the pause guard ever broke, these subprocesses would start the real pipeline. Blank every credential so a
# regression can neither send an email nor spend on Claude (empty values are not overridden by .env).
NO_CREDENTIALS = {"RESEND_API_KEY": "", "EMAIL_TO": "", "EMAIL_FROM": "", "ANTHROPIC_API_KEY": "",
                  "BIOTECH_ANTHROPIC_API_KEY": ""}


@pytest.fixture
def flag(tmp_path, monkeypatch):
    path = tmp_path / "PAUSED"
    monkeypatch.setenv("BIOTECH_PAUSE_FILE", str(path))
    return path


def test_pause_and_resume_round_trip(flag):
    assert not pause.is_paused() and "not paused" in pause.describe()
    pause.pause("away until November")
    assert pause.is_paused() and flag.exists()
    assert "PAUSED" in pause.describe() and "away until November" in pause.describe()
    assert pause.resume() is True and not pause.is_paused()
    assert pause.resume() is False  # resuming twice is harmless


def test_a_hand_made_empty_pause_file_reads_cleanly(flag):
    flag.write_text("")  # what `touch PAUSED` creates
    assert pause.describe().startswith("PAUSED. Resume with")


def run_daily(flag_path, *args):
    env = {**os.environ, **NO_CREDENTIALS, "BIOTECH_PAUSE_FILE": str(flag_path)}
    return subprocess.run([sys.executable, "-m", "app.run_daily", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_a_paused_daily_run_exits_zero_and_does_nothing(flag):
    flag.write_text("paused\n")
    result = run_daily(flag)
    assert result.returncode == 0  # cron sees success, not a failure
    assert "Paused" in result.stdout and "no email was sent" in result.stdout
    assert "Run complete" not in result.stdout  # it never reached the pipeline


def test_dry_run_still_works_while_paused(flag):
    flag.write_text("paused\n")
    result = run_daily(flag, "--dry-run")
    assert result.returncode == 0 and "Dry run. Steps that would run" in result.stdout


def test_not_paused_runs_normally(flag):
    result = run_daily(flag, "--dry-run")
    assert result.returncode == 0 and "Paused" not in result.stdout


def test_pause_command_line(flag):
    def cli(*args):
        env = {**os.environ, **NO_CREDENTIALS, "BIOTECH_PAUSE_FILE": str(flag)}
        return subprocess.run([sys.executable, "-m", "app.pause", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=30).stdout
    assert "not paused" in cli("--status")
    assert "Paused" in cli("away", "for", "now") and flag.exists()
    assert "away for now" in cli("--status")
    assert "Resumed" in cli("--resume") and not flag.exists()
    assert "It was not paused" in cli("--resume")  # a second resume is harmless
