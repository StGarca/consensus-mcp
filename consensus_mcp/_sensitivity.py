"""Sensitive-data policy shared by every path that launches a reviewer.

Seats whose profile sets ``sensitive_data_allowed: false`` (Kimi, Qwen) never
review a project that holds sensitive data. When nobody has said whether the
project does, the caller must ask the user rather than guess.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

# HTTP endpoints operated from China or Singapore. A seat served from one of
# these is restricted whatever its profile says (e.g. DeepSeek is trusted on a
# local or other-region server, but not on DeepSeek's own API).
RESTRICTED_HOST_SUFFIXES = (
    "deepseek.com", "aliyuncs.com", "aliyun.com", "alibabacloud.com",
    "qwencloudapi.com", "moonshot.cn", "moonshot.ai", "kimi.com", "kimi.ai",
)

ENV = "CONSENSUS_MCP_SENSITIVE_DATA"
QUESTION = (
    "Does this project contain sensitive data (private, personal, customer or "
    "company-confidential code or data)? Answer yes to leave the {seats} seat(s) "
    "out of this run, or no to include them.")


def builtin_restricted() -> frozenset:
    """Built-in seats restricted by policy. Project config cannot relax these:
    a self-hosted endpoint for the same model needs its own seat name."""
    from consensus_mcp._contributor_profiles import load_builtin_profiles
    return frozenset(name for name, profile in load_builtin_profiles().items()
                     if not isinstance(profile, dict)
                     or ("sensitive_data_allowed" in profile
                         and _allowed_flag_restricts(profile.get("sensitive_data_allowed"))))


def _endpoint_host(base) -> str | None:
    """Host of an endpoint, accepting URLs written without a scheme
    ("api.example.com/v1"). None when a non-empty value has no usable host."""
    text = str(base).strip()
    if "://" not in text and not text.startswith("//"):
        text = "//" + text
    try:
        host = urlparse(text).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def endpoint_restricted(profile) -> bool:
    """True when an HTTP seat's endpoint is a China/Singapore provider.
    An endpoint that is set but cannot be parsed counts as restricted."""
    if not isinstance(profile, dict):
        return False
    base = profile.get("base_url") or os.environ.get(profile.get("base_url_env") or "", "")
    if not base or not str(base).strip():
        return False
    host = _endpoint_host(base)
    if not host:
        return True
    return host.endswith(".cn") or any(host == s or host.endswith("." + s)
                                       for s in RESTRICTED_HOST_SUFFIXES)


def _allowed_flag_restricts(value) -> bool:
    """``sensitive_data_allowed`` restricts the seat unless it clearly says
    yes; text spellings such as "false"/"no" and anything unreadable restrict."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, str) and value.strip().lower() in {"true", "yes"}:
        return False
    return True


def is_restricted(profile, name=None) -> bool:
    if name is not None and name in builtin_restricted():
        return True
    if profile is None:
        return False
    if not isinstance(profile, dict):
        return True  # malformed profile: fail closed
    if endpoint_restricted(profile):
        return True
    return "sensitive_data_allowed" in profile and _allowed_flag_restricts(
        profile.get("sensitive_data_allowed"))


def normalize(value):
    """Return "yes", "no" or None (unanswered); raise ValueError otherwise."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str) and value.strip().lower() in {"yes", "no", "true", "false"}:
        return "yes" if value.strip().lower() in {"yes", "true"} else "no"
    raise ValueError("sensitive_data must be yes or no")


def standing_answer(config) -> str | None:
    """The answer a launched seat may rely on: the run's answer passed down by
    the consult panel (environment), else the project's ``sensitive_data``."""
    env = os.environ.get(ENV)
    if env:
        return normalize(env)
    return normalize(config.get("sensitive_data")) if isinstance(config, dict) else None


def question(seats) -> str:
    return QUESTION.format(seats=", ".join(seats))
