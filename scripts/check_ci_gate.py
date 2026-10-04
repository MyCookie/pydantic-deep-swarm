"""Fail the aggregate Actions check unless every declared dependency succeeds."""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", nargs="+", required=True, help="Required Actions job IDs")
    args = parser.parse_args()
    try:
        needs = json.load(sys.stdin)
    except (ValueError, OSError) as error:
        print(f"PR gate blocked: invalid dependency inventory ({error})", file=sys.stderr)
        return 1
    if (not isinstance(needs, dict) or set(needs) != set(args.expected)
            or any(not isinstance(job, dict) or "result" not in job for job in needs.values())):
        print("PR gate blocked: invalid dependency inventory", file=sys.stderr)
        return 1
    failures = [name for name in args.expected if needs[name]["result"] != "success"]
    if failures:
        print("PR gate blocked: " + ", ".join(failures), file=sys.stderr)
        return 1
    print("PR gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
