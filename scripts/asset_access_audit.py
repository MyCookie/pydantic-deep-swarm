"""Install fixture-only file access denial for the derived asset-absence lane."""

from __future__ import annotations

import json
from pathlib import Path


def install_audit(site_packages: Path, log: Path, forbidden_roots: list[Path]) -> Path:
    target = site_packages / "sitecustomize.py"
    if target.exists():
        raise RuntimeError("wheel fixture already has sitecustomize; do not overwrite it")
    roots = [str(path.resolve()) for path in forbidden_roots]
    # This applies only to installed console children. The controller itself
    # must still construct/inventory the deliberately removed fixture assets.
    source = '''import json, os, sys
if os.path.basename(sys.argv[0]) == "agent-team":
    _log = LOG_PATH
    _forbidden = ROOTS
    _busy = False
    def _audit(event, args):
        global _busy
        if _busy or event not in {"open", "subprocess.Popen"}:
            return
        _busy = True
        try:
            denied = False
            item = {"event": event, "pid": os.getpid()}
            if event == "open" and isinstance(args[0], (str, bytes)):
                path = os.path.realpath(os.fsdecode(args[0]))
                item["path"] = path
                denied = any(path == root or path.startswith(root + os.sep) for root in _forbidden)
                denied = denied or any(part in {".hermes", "s6-service"} for part in path.split(os.sep))
                denied = denied or "/integrations/hermes/" in path
            elif event == "subprocess.Popen":
                argv = args[1]
                if isinstance(argv, (list, tuple)) and argv:
                    executable = os.path.basename(os.fsdecode(argv[0]))
                    item["executable"] = executable
                    if executable == "git":
                        offset = 3 if len(argv) > 3 and argv[1] == "-C" else 1
                        operation = str(argv[offset]) if len(argv) > offset else ""
                        item["operation"] = operation
                        denied = operation not in {"rev-parse", "status"}
                    else:
                        denied = True
            item["denied"] = denied
            fd = os.open(_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(item) + "\\n").encode())
            finally:
                os.close(fd)
            if denied:
                raise PermissionError("fixture forbids optional assets or source fallback")
        finally:
            _busy = False
    sys.addaudithook(_audit)
'''.replace("LOG_PATH", repr(str(log))).replace("ROOTS", repr(roots))
    target.write_text(source)
    return target


def read_audit(log: Path) -> dict:
    events = [json.loads(line) for line in log.read_text().splitlines()]
    if not events:
        raise RuntimeError("optional-asset access audit did not observe installed children")
    return {"events": events, "pids": sorted({event["pid"] for event in events}),
            "denied_attempts": sum(event["denied"] for event in events),
            "claim": "Installed core console children deny Python file reads of optional assets/source fallback and arbitrary child commands; Git probes restricted to metadata operations."}
