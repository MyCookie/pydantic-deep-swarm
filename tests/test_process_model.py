"""Supported single-runtime process model tests."""

from __future__ import annotations

import json

import pytest

from agent_team.persistence import RuntimeAlreadyRunningError, RuntimeLease


def test_runtime_lease_is_exclusive_and_reusable_after_release(tmp_path):
    lock_path = tmp_path / "runtime.lock"
    first = RuntimeLease(lock_path)
    second = RuntimeLease(lock_path)

    first.acquire()
    try:
        with pytest.raises(RuntimeAlreadyRunningError):
            second.acquire()
        owner = json.loads((tmp_path / "runtime-owner.json").read_text())
        assert owner["pid"] > 0
        assert owner["lock_path"] == str(lock_path)
    finally:
        first.release()

    second.acquire()
    second.release()


def test_runtime_lease_release_is_idempotent(tmp_path):
    lease = RuntimeLease(tmp_path / "runtime.lock")
    lease.release()
    lease.acquire()
    lease.release()
    lease.release()
    assert not (tmp_path / "runtime-owner.json").exists()
