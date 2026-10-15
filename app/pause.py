"""Indefinite pause for the scheduled daily run.

While the file PAUSED exists in the project folder, `python -m app.run_daily` exits straight away: nothing is
fetched, no Claude calls are made, and no email is sent. Cron or Task Scheduler can stay set up.

  python -m app.pause "away until November"    pause (the note is optional)
  python -m app.pause --status                 show whether it is paused
  python -m app.pause --resume                 remove the pause

You can also pause by hand with `touch PAUSED` and resume with `rm PAUSED`.
To override the pause for one manual run:  python -m app.run_daily --force
"""
import argparse
import os
from datetime import datetime

from app.config import get_env, project_path


def pause_path():
    """The pause file. BIOTECH_PAUSE_FILE overrides the location (used by the tests)."""
    return get_env("BIOTECH_PAUSE_FILE") or project_path("PAUSED")


def is_paused():
    return os.path.exists(pause_path())


def pause(reason=""):
    """Create the pause file, remembering when and why. Returns its path."""
    path = pause_path()
    with open(path, "w") as f:
        f.write(f"paused {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}\n{reason}\n")
    return path


def resume():
    """Remove the pause file. Returns True if it was paused."""
    if not is_paused():
        return False
    os.remove(pause_path())
    return True


def describe():
    """One line saying whether the pipeline is paused, and why."""
    if not is_paused():
        return "running (not paused)"
    with open(pause_path()) as f:
        detail = " ".join(f.read().split())
    note = f" ({detail})" if detail else ""
    return f"PAUSED{note}. Resume with: python -m app.pause --resume"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pause or resume the scheduled daily run.")
    parser.add_argument("reason", nargs="*", help="optional note about why you are pausing")
    parser.add_argument("--resume", action="store_true", help="remove the pause")
    parser.add_argument("--status", action="store_true", help="show whether it is paused")
    args = parser.parse_args()
    if args.status:
        print(describe())
    elif args.resume:
        print("Resumed. The next scheduled run will proceed normally." if resume() else "It was not paused.")
    else:
        print(f"Paused: {pause(' '.join(args.reason))}\nScheduled runs now do nothing until: python -m app.pause --resume")
