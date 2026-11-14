"""Small sealed-review fixtures for delivery gate tests."""
import yaml

from consensus_mcp.tools.review_write_and_seal import _canonical_yaml_sha256


def write_sealed_reviews(iteration, names=("codex", "gemini", "kimi")):
    for name in names:
        packet = dict(iteration_id=iteration.name, reviewer_id=name, findings=[],
                      goal_satisfied=True, sealed_at_utc="2026-11-13T00:00:00Z")
        packet["packet_sha256"] = _canonical_yaml_sha256(packet)
        (iteration / f"{name}-review.yaml").write_text(yaml.safe_dump(packet), encoding="utf-8")
