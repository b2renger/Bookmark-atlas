# ruff: noqa: E501
"""Does the model server end when the app is gone, on this system?

Run by the release workflow on each platform with the bundled interpreter:
    python -I guard-selfcheck.py <path to backend/app>
A stand-in for the app starts a long-lived child under atlas.engine's guard,
is then killed without warning, and the child must be gone within ten seconds.
Not a gate: read its log.
"""

import os
import subprocess
import sys
import time

app = sys.argv[1]
parent = (
    "import subprocess, sys, time\n"
    f"sys.path.insert(0, {app!r})\n"
    "from atlas import engine\n"
    "child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])\n"
    "guard = engine._guard(child)\n"
    "print(child.pid, guard.pid if guard else 0, flush=True)\n"
    "time.sleep(120)\n"
)


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in listed
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # ended, not yet collected
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


started = subprocess.Popen([sys.executable, "-I", "-c", parent], stdout=subprocess.PIPE, text=True)
child, guard = (int(n) for n in started.stdout.readline().split())
print(
    f"platform {sys.platform} | child {child} alive: {alive(child)} | guard started: {bool(guard)}"
)
started.kill()  # no goodbye: what the shell does when the window closes
started.wait()
end = time.time() + 10
while time.time() < end and alive(child):
    time.sleep(0.2)
took = 10 - max(0.0, end - time.time())
print(
    f"after the app was killed: child alive: {alive(child)} (looked for {took:.1f} s) | guard alive: {alive(guard) if guard else None}"
)
sys.exit(1 if alive(child) else 0)
