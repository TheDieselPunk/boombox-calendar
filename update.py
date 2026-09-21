"""Task Scheduler entry point (Windows). Mirrors update.sh.

Launch it with pythonw.exe, which has no console, and every child process is
started with CREATE_NO_WINDOW - so nothing flashes on screen when the task runs.
All output goes to update.log next to this file. Any failure stops before the
commit, so the previously published feeds stay in place.

    "C:\\...\\Python313\\pythonw.exe" "C:\\...\\miami-calendars\\update.py"
"""

import datetime as dt
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "update.log")
CREATE_NO_WINDOW = 0x08000000
# pythonw.exe is fine for this launcher, but run the build with the console
# interpreter so its stdio behaves normally.
PYTHON = os.path.join(os.path.dirname(sys.executable), "python.exe")


def log(text):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def run(*cmd, check=True):
    with open(LOG, "a", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=HERE, stdout=fh, stderr=subprocess.STDOUT,
                              creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    if check and proc.returncode:
        log(f"!! {' '.join(cmd)} exited {proc.returncode}")
        sys.exit(proc.returncode)
    return proc.returncode


def main():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    log(f"== {stamp}")
    run("git", "pull", "--rebase", "--autostash", "--quiet")
    run(PYTHON if os.path.exists(PYTHON) else sys.executable, "build_feeds.py")
    run("git", "add", "*.ics", "events.json", "feeds.json", "state.json")
    if run("git", "diff", "--cached", "--quiet", check=False) == 0:
        log("Feeds unchanged")
        return 0
    run("git", "commit", "--quiet", "-m", f"Update feeds {stamp}")
    run("git", "push", "--quiet")
    log("Pushed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pythonw has no stderr; make sure it lands in the log
        log(f"FAILED: {exc!r}")
        sys.exit(1)
