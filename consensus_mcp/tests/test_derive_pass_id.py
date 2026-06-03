"""Tests for derive_pass_id: the default dispatch pass_id is a deterministic hash
of (iteration, packet, contributor) so it is GLOBALLY unique in the T6 seal index.

Why: the T6 seal index is a global pass_id namespace (content-identity tamper
guard). The old default `<reviewer>-pass1` is identical across every iteration, so
two consults collide with a cryptic index_collision (observed twice in one session
2026-06-02). Operator design: make the default pass_id the hash of iteration# +
packet name + contributor name -- deterministic-unique AND idempotent for an
identical re-dispatch.
"""
from __future__ import annotations

import re

from consensus_mcp._dispatch_base import derive_pass_id


def test_deterministic_same_inputs():
    a = derive_pass_id("iter-A", "/some/dir/review-packet.yaml", "grok")
    b = derive_pass_id("iter-A", "review-packet.yaml", "grok")  # basename only
    assert a == b


def test_unique_across_iterations():
    assert derive_pass_id("iter-A", "review-packet.yaml", "grok") != \
           derive_pass_id("iter-B", "review-packet.yaml", "grok")


def test_unique_across_contributors():
    assert derive_pass_id("iter-A", "review-packet.yaml", "grok") != \
           derive_pass_id("iter-A", "review-packet.yaml", "kimi")


def test_unique_across_packets():
    assert derive_pass_id("iter-A", "review-packet.yaml", "grok") != \
           derive_pass_id("iter-A", "converge-round-2.yaml", "grok")


def test_readable_prefix_and_filesystem_safe():
    pid = derive_pass_id("iter-A", "review-packet.yaml", "grok")
    assert pid.startswith("grok-")            # readable: <reviewer>-<hash>
    assert re.fullmatch(r"[A-Za-z0-9._-]+", pid)  # safe for sealed-mirror filename


def test_tolerates_none_packet():
    pid = derive_pass_id("iter-A", None, "grok")
    assert pid.startswith("grok-") and re.fullmatch(r"[A-Za-z0-9._-]+", pid)
