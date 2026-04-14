"""Tests for the sandbox entrypoint's env-var handling and OCI config build."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_ENTRYPOINT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "helpers" / "entrypoint.py"
)


def _load_entrypoint() -> types.ModuleType:
    """Load entrypoint.py as a module without executing its __main__ block."""
    spec = importlib.util.spec_from_file_location("_entrypoint_under_test", _ENTRYPOINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Suppress sys.argv side effects that would otherwise leak from the test runner.
    saved_argv = sys.argv
    sys.argv = ["entrypoint.py"]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = saved_argv
    return module


entrypoint = _load_entrypoint()
build_oci_config = entrypoint.build_oci_config
ALLOWED_ENV: frozenset[str] = entrypoint.ALLOWED_ENV


def _env_dict(config: dict) -> dict[str, str]:
    """Parse ``process.env`` list-of-``KEY=VAL`` strings into a dict."""
    return dict(entry.split("=", 1) for entry in config["process"]["env"])


class TestAllowlistContents:
    """The allowlist itself is part of the security contract; pin it down."""

    def test_contains_only_locale_and_tz(self) -> None:
        assert ALLOWED_ENV == {"LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE", "TZ"}

    def test_excludes_sandbox_control_vars(self) -> None:
        # These env vars are consumed by the entrypoint itself and must never
        # be forwarded into the sandbox, either by name or by value.
        for var in ("RUNSC_DEBUG", "RUNSC_FLAGS"):
            assert var not in ALLOWED_ENV


class TestOCIConfigBaseline:
    """The baseline OCI config is the same regardless of hostile env."""

    def test_baseline_env_always_set(self) -> None:
        config = build_oci_config(["sh"], {})
        env = _env_dict(config)
        assert env["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        assert env["PYTHONPATH"] == "/opt/dangerzone"
        assert env["TERM"] == "xterm"

    def test_args_are_set_from_command(self) -> None:
        config = build_oci_config(["/opt/dangerzone/convert.py", "--flag"], {})
        assert config["process"]["args"] == ["/opt/dangerzone/convert.py", "--flag"]

    def test_empty_env_produces_only_baseline(self) -> None:
        config = build_oci_config(["sh"], {})
        assert set(_env_dict(config).keys()) == {"PATH", "PYTHONPATH", "TERM"}

    def test_returns_independent_copies(self) -> None:
        c1 = build_oci_config(["sh"], {})
        c2 = build_oci_config(["sh"], {})
        c1["process"]["env"].append("MUTATED=1")
        assert "MUTATED=1" not in _env_dict(c2)


class TestOCIConfigForwardsAllowlistOnly:
    """The final config.process.env array is the real blast site."""

    def test_forwards_locale_and_tz(self) -> None:
        env = {"LANG": "en_US.UTF-8", "LC_ALL": "C", "LC_CTYPE": "UTF-8",
               "LANGUAGE": "en", "TZ": "UTC"}
        config = build_oci_config(["sh"], env)
        forwarded = _env_dict(config)
        for key, val in env.items():
            assert forwarded[key] == val

    def test_drops_sensitive_vars_by_name(self) -> None:
        hostile = {
            "AWS_SECRET_ACCESS_KEY": "AKIA...",
            "AWS_SESSION_TOKEN": "...",
            "GITHUB_TOKEN": "ghp_...",
            "ANTHROPIC_API_KEY": "sk-ant-...",
            "SSH_AUTH_SOCK": "/tmp/ssh-XXX/agent",
            "GPG_AGENT_INFO": "/tmp/gpg-XXX",
            "KUBECONFIG": "/home/user/.kube/config",
        }
        config = build_oci_config(["sh"], hostile)
        # Drop semantics: neither the name NOR the value appears anywhere.
        serialized = "\n".join(config["process"]["env"])
        for key, val in hostile.items():
            assert key not in serialized, f"{key} leaked into config"
            assert val not in serialized, f"value for {key} leaked into config"

    def test_drops_sandbox_control_vars(self) -> None:
        # These vars configure the entrypoint itself; they must never reach
        # the sandboxed process, or a malicious upstream env could influence
        # the inside of the sandbox.
        config = build_oci_config(["sh"], {"RUNSC_DEBUG": "1", "RUNSC_FLAGS": "-x"})
        env_keys = set(_env_dict(config).keys())
        assert "RUNSC_DEBUG" not in env_keys
        assert "RUNSC_FLAGS" not in env_keys

    def test_drops_unknown_vars(self) -> None:
        config = build_oci_config(["sh"], {"MY_CUSTOM_VAR": "x", "FOO": "bar"})
        env_keys = set(_env_dict(config).keys())
        assert "MY_CUSTOM_VAR" not in env_keys
        assert "FOO" not in env_keys

    def test_mixed_env_forwards_only_allowlisted(self) -> None:
        env = {
            "LANG": "en_US.UTF-8",
            "HOME": "/home/user",
            "PATH": "/usr/local/bin",  # parent's PATH must not override baseline.
            "AWS_SECRET_ACCESS_KEY": "AKIA...",
            "TZ": "America/Los_Angeles",
        }
        config = build_oci_config(["sh"], env)
        forwarded = _env_dict(config)
        # Allowlisted vars carry through.
        assert forwarded["LANG"] == "en_US.UTF-8"
        assert forwarded["TZ"] == "America/Los_Angeles"
        # PATH is the baseline, not the parent's value.
        assert forwarded["PATH"] == (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        # Non-allowlisted vars are gone.
        assert "HOME" not in forwarded
        assert "AWS_SECRET_ACCESS_KEY" not in forwarded

    def test_parent_cannot_shadow_baseline(self) -> None:
        # Even if a parent set PATH/PYTHONPATH/TERM, those aren't in the
        # allowlist, so the baseline wins and there are no duplicates.
        env = {"PATH": "/evil", "PYTHONPATH": "/evil", "TERM": "evil"}
        config = build_oci_config(["sh"], env)
        path_entries = [e for e in config["process"]["env"] if e.startswith("PATH=")]
        assert path_entries == [
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ]


@pytest.mark.parametrize(
    "hostile_value",
    [
        "",
        "value with spaces",
        "value\nwith\nnewlines",
        "value\x00with\x00nulls",
        "=starts-with-equals",
        "value=with=equals",
        "'; rm -rf /; echo '",
    ],
    ids=["empty", "spaces", "newlines", "nulls", "leading-eq", "embedded-eq", "shell-inj"],
)
def test_hostile_values_dropped_when_key_not_allowlisted(hostile_value: str) -> None:
    config = build_oci_config(["sh"], {"INJECT": hostile_value})
    serialized = "\n".join(config["process"]["env"])
    assert "INJECT" not in serialized
    if hostile_value:
        assert hostile_value not in serialized


def test_allowlisted_key_preserves_value_verbatim() -> None:
    # Values for allowlisted keys are forwarded without sanitization -- that's
    # the container runtime's job. Pin that contract so we notice if it changes.
    config = build_oci_config(["sh"], {"LANG": "en_US.UTF-8@with/weird=chars"})
    assert _env_dict(config)["LANG"] == "en_US.UTF-8@with/weird=chars"
