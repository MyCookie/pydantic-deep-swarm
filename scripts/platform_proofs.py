"""Closed registry of host-specific proofs; all other tests remain required."""

NATIVE_PROOFS = {
    "tests/test_child_confinement.py::test_darwin_detached_child_capability_boundary": "darwin",
    "tests/test_forced_child_foreground.py::test_forced_foreground_preserves_durable_authority_with_surviving_child[shutdown-timeout]": "darwin",
    "tests/test_forced_child_foreground.py::test_forced_foreground_preserves_durable_authority_with_surviving_child[second-signal]": "darwin",
}
