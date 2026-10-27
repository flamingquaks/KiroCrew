"""An app-bound profile withholds static grants only from agents its app can run.

``any_configured_profile_governs`` decides auto-approve at spec-write time. Given
the spec's name, it leaves out an app-bound profile whose app cannot put that
agent under its context: spawn is off, the app is not an in-process runner, and
the app did not register the agent. Every other bind, and every unknown agent,
still counts, and the builtin floor is untouched.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.agent_materialization import fork_refresh
from kiro_crew.apps import manager as app_manager
from kiro_crew.apps import manager_lookup
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.platform import governance as gov
from kiro_crew.platform import governance_profiles as gp

#: The shape of an app profile that switches spawn off and confines egress.
_TUNNEL = {
    "name": "app-tunnel",
    "bind": {"type": "app", "id": "tunnel"},
    "capabilities": {"spawn": {"enabled": False}, "cron": {"enabled": False}},
    "network": {"egress": {"mode": "allow", "allow": []}},
}

_FLOOR_BUILTINS = ("fs_read", "fs_write", "execute_bash", "code", "grep", "glob")


class _NoCeiling:
    governance = None


@pytest.fixture
def profiles(monkeypatch):
    """Install *profiles* as the loaded store, with no policy ceiling."""
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: _NoCeiling())
    monkeypatch.setattr(gov, "_operator_requires_confirmation", lambda ref: False)
    monkeypatch.setattr(gp._STORE, "resolved", lambda: True)

    def install(*docs: dict) -> None:
        loaded = [gov.parse_profile(doc) for doc in docs]
        monkeypatch.setattr(gp._STORE, "all_profiles", lambda: loaded)

    return install


#: The tunnel app's manifest: no backend, and one API grant outside ``/api/chat``.
_TUNNEL_MANIFEST = {
    "name": "tunnel",
    "version": "1.0.0",
    "permissions": {"api": ["/api/edition/tunnel"]},
}


@pytest.fixture
def manifest(monkeypatch):
    """Serve *doc* (or None, for an app with no readable manifest) as every app's manifest."""

    def install(doc: dict | None) -> None:
        parsed = AppManifest.from_dict(doc) if doc is not None else None
        monkeypatch.setattr(app_manager, "get_app_manifest", lambda name: parsed)

    return install


@pytest.fixture
def registration(monkeypatch, manifest):
    """Answer the app registration lookup with *status* for every (app, agent).

    Also serves the tunnel manifest, which cannot open chat sessions; a test
    about session-capable apps installs its own manifest afterwards.
    """

    def answer(status: str) -> None:
        manifest(_TUNNEL_MANIFEST)
        monkeypatch.setattr(
            manager_lookup, "_app_agent_registration", lambda app, agent: (status, ())
        )

    return answer


def _governed(config: dict, agent: str) -> list:
    fork_refresh._apply_governance_passes(config, source="test", agent=agent)
    return config["allowedTools"]


def test_the_tunnel_profile_governs_the_server_at_all(profiles) -> None:
    profiles(_TUNNEL)
    assert gp.any_configured_profile_governs("@builder-mcp") is True


def test_an_unrelated_agent_keeps_its_server_grants(profiles, registration) -> None:
    profiles(_TUNNEL)
    registration("missing")
    config = {"allowedTools": ["@builder-mcp", "@slack-mcp/search", "web_fetch"]}
    assert _governed(config, "amzn-builder") == ["@builder-mcp", "@slack-mcp/search", "web_fetch"]


def test_an_unnamed_agent_still_counts_every_profile(profiles, registration) -> None:
    profiles(_TUNNEL)
    registration("missing")
    assert gov.may_skip_gate_now("@builder-mcp") is False
    assert _governed({"allowedTools": ["@builder-mcp"]}, "") == []


@pytest.mark.parametrize("status", ["ok", "ambiguous", "unverifiable"])
def test_the_apps_own_agent_is_still_stripped(profiles, registration, status) -> None:
    profiles(_TUNNEL)
    registration(status)
    assert _governed({"allowedTools": ["@builder-mcp"]}, "tunnel-helper") == []


def test_a_namespaced_app_agent_is_stripped_without_a_lookup(profiles, monkeypatch) -> None:
    profiles(_TUNNEL)

    def _unreachable(app, agent):
        raise AssertionError("the namespace alone proves ownership")

    monkeypatch.setattr(manager_lookup, "_app_agent_registration", _unreachable)
    assert _governed({"allowedTools": ["@builder-mcp"]}, "tunnel--helper") == []


def test_an_ownership_lookup_that_raises_strips(profiles, monkeypatch) -> None:
    profiles(_TUNNEL)

    def _broken(app, agent):
        raise OSError("agents dir unreadable")

    monkeypatch.setattr(manager_lookup, "_app_agent_registration", _broken)
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


def test_an_app_that_may_spawn_governs_every_agent(profiles, registration) -> None:
    spawning = dict(_TUNNEL, capabilities={"cron": {"enabled": False}})
    profiles(spawning)
    registration("missing")
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


@pytest.mark.parametrize(
    "api",
    [
        ["/api/chat"],
        ["/api/chat/slots"],
        ["/api/chat/slots/*"],
        ["/api/*"],
        ["/api/c*"],
        ["*"],
        ["/api/edition/tunnel", "/api/chat/slots/{slot}/agent"],
    ],
)
def test_an_app_that_may_open_chat_sessions_governs_every_agent(
    profiles, registration, manifest, api
) -> None:
    # The app opens a slot on an agent it never registered; that slot's calls
    # carry the app's context, so the agent's spec must not pre-approve them.
    profiles(_TUNNEL)
    registration("missing")
    manifest(dict(_TUNNEL_MANIFEST, permissions={"api": api}))
    config = {"allowedTools": ["@builder-mcp", "web_fetch"]}
    assert _governed(config, "amzn-builder") == []
    auto = _with_auto_approve()
    fork_refresh._apply_governance_passes(auto, source="test", agent="amzn-builder")
    assert "autoApprove" not in auto["mcpServers"]["builder-mcp"]


def test_an_app_with_a_backend_governs_every_agent(profiles, registration, manifest) -> None:
    profiles(_TUNNEL)
    registration("missing")
    manifest(dict(_TUNNEL_MANIFEST, backend={"entryPoint": "backend/app.py"}))
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


def test_an_app_with_no_readable_manifest_governs_every_agent(
    profiles, registration, manifest
) -> None:
    profiles(_TUNNEL)
    registration("missing")
    manifest(None)
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


def test_a_session_capability_lookup_that_raises_strips(
    profiles, registration, monkeypatch
) -> None:
    profiles(_TUNNEL)
    registration("missing")

    def _broken(name):
        raise OSError("apps dir unreadable")

    monkeypatch.setattr(app_manager, "get_app_manifest", _broken)
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


def test_an_in_process_runner_app_governs_every_agent(profiles, registration) -> None:
    runner = dict(_TUNNEL, name="app-ai", bind={"type": "app", "id": "auto-improvement"})
    profiles(runner)
    registration("missing")
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


@pytest.mark.parametrize(
    "bind",
    [{"type": "surface", "id": "dashboard"}, {"type": "task", "id": "someone-else"}],
)
def test_surface_and_task_binds_stay_global(profiles, registration, bind) -> None:
    profiles(dict(_TUNNEL, name="other", bind=bind))
    registration("missing")
    assert _governed({"allowedTools": ["@builder-mcp"]}, "amzn-builder") == []


@pytest.mark.parametrize("with_profile", [False, True])
def test_the_builtin_floor_is_unchanged(profiles, registration, with_profile) -> None:
    profiles(*([_TUNNEL] if with_profile else []))
    registration("missing")
    config = {"allowedTools": [*_FLOOR_BUILTINS, "web_fetch"]}
    assert _governed(config, "amzn-builder") == ["web_fetch"]
    for ref in _FLOOR_BUILTINS:
        assert gov.may_skip_gate_now(ref, agent="amzn-builder") is False


def _with_auto_approve() -> dict:
    return {"mcpServers": {"builder-mcp": {"command": "builder-mcp", "autoApprove": ["*"]}}}


def test_an_unrelated_agent_keeps_its_server_auto_approve(profiles, registration) -> None:
    profiles(_TUNNEL)
    registration("missing")
    config = _with_auto_approve()
    fork_refresh._apply_governance_passes(config, source="test", agent="amzn-builder")
    assert config["mcpServers"]["builder-mcp"]["autoApprove"] == ["*"]


def test_the_apps_own_agent_loses_its_server_auto_approve(profiles, registration) -> None:
    profiles(_TUNNEL)
    registration("ok")
    config = _with_auto_approve()
    fork_refresh._apply_governance_passes(config, source="test", agent="tunnel-helper")
    assert "autoApprove" not in config["mcpServers"]["builder-mcp"]


def test_an_unnamed_server_map_still_counts_every_profile(profiles, registration) -> None:
    profiles(_TUNNEL)
    registration("missing")
    out = gov.strip_ungoverned_auto_approve(_with_auto_approve()["mcpServers"])
    assert "autoApprove" not in out["builder-mcp"]


@pytest.mark.parametrize(("status", "kept"), [("missing", True), ("ok", False)])
def test_the_kas_projection_scopes_by_agent(profiles, registration, status, kept) -> None:
    from kiro_crew.acp import kas_agents

    profiles(_TUNNEL)
    registration(status)
    permitted = kas_agents._ceiling_permitted(["@builder-mcp", *_FLOOR_BUILTINS], "amzn-builder")
    assert permitted == (["@builder-mcp"] if kept else [])


def test_the_kas_projection_governs_agents_a_chat_capable_app_can_run(
    profiles, registration, manifest
) -> None:
    from kiro_crew.acp import kas_agents

    profiles(_TUNNEL)
    registration("missing")
    manifest(dict(_TUNNEL_MANIFEST, permissions={"api": ["/api/chat"]}))
    assert kas_agents._ceiling_permitted(["@builder-mcp"], "amzn-builder") == []


_GATE_CALLS = frozenset({"on_tool_call", "judge", "refusal_for"})


def _callee_name(node: ast.expr) -> str:
    """The name a call or a forwarded callable is spelled with (``a.b`` -> ``b``)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def test_every_literal_app_context_at_the_gate_is_an_in_process_runner() -> None:
    """A new runner that passes ``app="<id>"`` to the gate must be listed."""
    root = Path(gp.__file__).resolve().parents[1]
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _callee_name(node.func)
            forwarded = called == "to_thread" and node.args and _callee_name(node.args[0])
            if called not in _GATE_CALLS and forwarded not in _GATE_CALLS:
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "app"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                    and kw.value.value
                ):
                    found.add(kw.value.value)
    assert found, "the scan found no app context; it is reading the wrong tree"
    # The code review app forwards refusal_for through asyncio.to_thread; a scan
    # matching only attribute calls would miss it.
    assert "code-review-sage" in found, found
    assert found <= gp.IN_PROCESS_AGENT_APP_CONTEXTS, found


def test_the_code_review_runner_app_governs_every_agent(profiles, registration) -> None:
    sage = dict(_TUNNEL, name="app-sage", bind={"type": "app", "id": "code-review-sage"})
    profiles(sage)
    registration("missing")
    assert _governed({"allowedTools": ["@builder-mcp"]}, "code-review-sage-reviewer") == []
