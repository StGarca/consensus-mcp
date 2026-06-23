"""Vendored Looper design-coach slice (MIT, (c) Kevin Simback). See VENDORED.md.

This package coaches a Build goal (goal + typed verification + caps) and seeds
Consensus Build. It is imported ONLY on the with-looper-plan goal-setup path -
never by the Build supervisor (architect.loop_step), to preserve the zero-diff
guarantee. Public API (compile_plan, synthesize_stub_fields, seed_build_inputs)
is bound in compile.py / seed.py; import those submodules directly.
"""
from consensus_mcp.looper_plan.compile import compile_plan, synthesize_stub_fields
from consensus_mcp.looper_plan.seed import seed_build_inputs

__all__ = ["compile_plan", "synthesize_stub_fields", "seed_build_inputs"]
