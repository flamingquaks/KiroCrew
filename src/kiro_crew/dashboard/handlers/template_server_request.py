"""A template-based session asks for an MCP server (``request_server``), and the card that adds it.

Three routes, split the way the change cards are:

* ``POST /api/cards/agent/request-server`` -- the agent half, MCP-only under the
  strict ``/api/cards/agent`` prefix. The session it acts for is the verified
  caller's own (:func:`handlers.guide._resolve_agent_caller`, a user-turn
  operation), never a name from the body, and so is the template: the one that
  session runs on, read from its execution record. A cron, a subagent, a
  messaging channel, a patrol or any other turn the user did not send is
  refused with the reason, so no card waits where nobody sees it. A crewmate
  asks with ``request_capabilities`` instead. The server is resolved by the
  gateway (:func:`member_capability_catalog.resolve_server`): a server this
  gateway already runs, or the official registry's install plan -- never a
  launch line from the request. On a backend where an addition cannot reach the
  template's sessions (:func:`template_mcp_additions.addition_support`) the
  request is refused with that reason. The card is proposed through
  :func:`handlers.change_cards.propose_for_caller`, the body ``propose_change``
  runs, and only the owner's browser can apply it.
* ``POST /api/agents/detail/{name}/mcp-additions`` and
  ``DELETE /api/agents/detail/{name}/mcp-additions/{server}`` -- the browser half,
  owner-only cookie auth, hooked by the card engine. The POST is a card step
  only (it records the card it ran under); the DELETE is that card's Undo, and
  with a ``card_id`` removes the addition only when that card recorded it.

After either write the agent specs are rebuilt (the installers merge the record)
and running sessions pick the change up live where every live process can
(``mcp_hot_reload``), else they are reset, exactly as an MCP page change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers.guide import _audit, _deny, _json_body, _resolve_agent_caller

logger = logging.getLogger(__name__)

#: ``request_server`` operation name, in :data:`handlers.guide._USER_TURN_OPERATIONS`.
OP_REQUEST_SERVER = "cards.request_server"

_BODY_KEYS = frozenset({"server", "source", "registry_id", "reason"})

#: What each caller refusal tells the agent: the reason, in this tool's words, so
#: it can tell the user plainly instead of retrying from a turn nobody sees.
_CALLER_REFUSALS: dict[str, str] = {
    "subagent_caller": (
        "a subagent cannot raise a card; tell your parent session which server the "
        "work needs and let it ask"
    ),
    "unattended_caller": (
        "a scheduled run has nobody watching to approve a card; say in your result "
        "which server the work needs"
    ),
    "channel_caller": (
        "this message came from a messaging channel, and a card is approved only in "
        "the dashboard; tell the user which server the work needs"
    ),
    "not_user_turn": (
        "only a message the user sent in the dashboard can raise this card, not a "
        "patrol, a wake, a schedule or another session's message; say which server "
        "the work needs and ask when the user writes next"
    ),
    "no_dashboard_turn": "this request did not come from a message sent in the dashboard",
    "no_live_slot": "this session is not open in a dashboard tab, so nobody would see the card",
}


def _caller_refusal(exc: GuideError) -> web.Response:
    message = _CALLER_REFUSALS.get(exc.code, exc.message.replace("guide", "card"))
    status = exc.status if exc.status in (400, 403, 404, 409, 429) else 400
    return _deny(status, exc.code, message)


# ── who is asking, and for which template ──


def _caller_template(state: Any, slot_key: str, sk: str) -> tuple[str, str, str]:
    """``(template, code, message)``: the template the caller runs on, or why there is none.

    Blocking (reads the session's execution record).
    """
    from kiro_crew import mate_worker, members
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread
    from kiro_crew.execution_context import read_session_execution

    slot = state.get_slot(slot_key)
    if (
        is_crewmate_thread(slot)
        or members.is_crewmate_worker_session(slot_key)
        or members.is_crewmate_worker_session(sk)
    ):
        return (
            "",
            "crewmate_caller",
            "a crewmate asks for its worker's servers with request_capabilities",
        )
    try:
        execution = read_session_execution(slot_history_key(slot))
    except Exception:  # noqa: BLE001 - an unreadable record names no template
        logger.debug("request_server: execution unreadable for %s", slot_key, exc_info=True)
        execution = None
    template = getattr(execution, "template_id", "") if execution is not None else ""
    if not isinstance(template, str) or not template:
        return (
            "",
            "no_template",
            "this session runs on no agent template a server can be added to",
        )
    if mate_worker.is_mate_owned_agent(template):
        return (
            "",
            "crewmate_caller",
            "a crewmate asks for its worker's servers with request_capabilities",
        )
    return template, "", ""


async def _caller_backend(state: Any, sk: str) -> str:
    """The backend the caller's turn runs on (``""`` is Kiro's own id)."""
    from kiro_crew.dashboard.chat_turn.app_agent_binding import _app_agent_turn_backend

    try:
        backend = await _app_agent_turn_backend(state, sk, None)
    except Exception:  # noqa: BLE001 - unknown is treated as the default harness
        logger.debug("request_server: backend unreadable for %s", sk, exc_info=True)
        backend = None
    return backend or ""


# ── reach: which templates, sessions and members one addition reaches ──


def addition_reach_view(state: Any, template: str) -> dict[str, list[str]]:
    """The templates, open sessions and crew members an addition to *template* reaches.

    Templates come from :func:`template_mcp_reach.addition_reach`; each open
    dashboard session is placed by its own execution record. Blocking.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.template_mcp_reach import addition_reach

    templates = list(addition_reach(template))
    wanted = set(templates)
    sessions: list[str] = []
    for slot in list(getattr(state, "_slots", {}).values()):
        try:
            execution = read_session_execution(slot_history_key(slot))
        except Exception:  # noqa: BLE001 - an unplaceable session is not listed
            continue
        if execution is not None and getattr(execution, "template_id", "") in wanted:
            sessions.append(str(getattr(slot, "title", "") or getattr(slot, "key", "")))
    try:
        agents = KiroCrewConfig.load().agents
    except Exception:  # noqa: BLE001 - the card still lists templates and sessions
        agents = {}
    members = sorted(
        str(getattr(cfg, "display_name", "") or crew)
        for crew, cfg in agents.items()
        if getattr(cfg, "kiro_agent", "") in wanted
    )
    return {"templates": templates, "sessions": sorted(sessions), "members": members}


def live_apply(state: Any) -> bool:
    """Whether every live process applies an MCP change without a reset. Never marks."""
    from kiro_crew.mcp_hot_reload import live_sessions_apply_mcp_live

    try:
        sessions = state.sessions
        providers = list(sessions.active_providers()) + list(sessions.warm_providers())
        return bool(live_sessions_apply_mcp_live(providers))
    except Exception:  # noqa: BLE001 - fail closed: the card says sessions restart
        return False


def template_declares(template: str, server: str) -> bool:
    """Whether *template*'s own spec already declares *server* (not via an addition). Blocking."""
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

    if server in additions.read_additions(template):
        return False
    try:
        candidates = _agent_detail_candidates(template)
    except Exception:  # noqa: BLE001 - unreadable: the record's add-only merge still holds
        return False
    for _path, spec in candidates:
        servers = spec.get("mcpServers") if isinstance(spec, dict) else None
        if isinstance(servers, dict) and server in servers:
            return True
    return False


# ── agent half ──


async def api_cards_agent_request_server(request: web.Request) -> web.Response:
    """POST /api/cards/agent/request-server — ``{server?, source, registry_id?, reason?}`` → Card."""
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.change_card_catalog import KIND_TEMPLATE_MCP_SERVER
    from kiro_crew.dashboard.handlers.change_cards import propose_for_caller
    from kiro_crew.dashboard.handlers.member_capability_catalog import resolve_server
    from kiro_crew.mate_capability_catalog import AdditionError as CatalogAdditionError
    from kiro_crew.template_mcp_reach import record_template

    try:
        slot_key, sk = await _resolve_agent_caller(request, OP_REQUEST_SERVER)
        body = await _json_body(request)
    except GuideError as exc:
        return _caller_refusal(exc)
    unknown = sorted(set(body) - _BODY_KEYS)
    if unknown:
        # A command, url, template or session is never the caller's to give.
        _audit(sk, OP_REQUEST_SERVER, "denied", "unknown_field")
        return _deny(400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
    state = request.app["state"]
    template, code, message = await asyncio.to_thread(_caller_template, state, slot_key, sk)
    if not template:
        _audit(sk, OP_REQUEST_SERVER, "denied", code)
        return _deny(403 if code == "crewmate_caller" else 409, code, message)
    template = record_template(template)
    support = additions.addition_support(template, await _caller_backend(state, sk))
    if not support.supported:
        _audit(sk, OP_REQUEST_SERVER, "denied", "unsupported")
        return _deny(409, "unsupported", support.reason)
    try:
        name, transport, origin = await resolve_server(
            body.get("server"),
            body.get("source"),
            body.get("registry_id"),
            settings_hint="ask the user to add it on the MCP page instead",
        )
    except CatalogAdditionError as exc:
        _audit(sk, OP_REQUEST_SERVER, "denied", exc.code)
        return _deny(400, exc.code, exc.message)
    try:
        if transport is None:
            raise additions.AdditionError("entry_not_object")
        entry = additions.validate_entry(transport)
    except additions.AdditionError as exc:
        _audit(sk, OP_REQUEST_SERVER, "denied", "entry_not_recordable")
        return _deny(
            400,
            "entry_not_recordable",
            f"{name}'s entry cannot be added to an agent ({exc}); ask the user to add it "
            "on the MCP page instead",
        )
    return await propose_for_caller(
        request,
        slot_key,
        sk,
        kind=KIND_TEMPLATE_MCP_SERVER,
        params={"template": template, "server": name, "entry": entry, "origin": origin},
        reason=body.get("reason"),
    )


# ── browser half (card steps) ──


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    """403 ``owner_only`` unless this is the owner's own browser (never an internal caller)."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    if request.get("internal_auth") is True:
        _audit(
            str(request.get("user") or "internal"),
            operation,
            "denied",
            "internal caller",
        )
        return _deny(403, "owner_only", "only the dashboard owner can change an agent's servers")
    return await require_owner_dashboard_request(request, operation)


async def _apply_to_sessions(request: web.Request) -> dict[str, Any]:
    """Rebuild the agent specs, then apply live or reset, as an MCP page change does."""
    from kiro_crew.agent_parts.spec_rebuild import rebuild_agent_config_reporting
    from kiro_crew.dashboard.handlers.mcp_agent_sync import _mcp_hot_reload_active

    rebuilt = False
    try:
        _path, rebuilt = await asyncio.to_thread(rebuild_agent_config_reporting)
    except Exception:  # noqa: BLE001 - the record holds; the next rebuild merges it
        logger.warning("agent rebuild after a template MCP addition failed", exc_info=True)
    if _mcp_hot_reload_active(request):
        return {"rebuilt": rebuilt, "sessions_reset": 0}
    from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

    return {"rebuilt": rebuilt, "sessions_reset": await _reset_all_sessions(request)}


async def api_template_mcp_addition_add(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/mcp-additions — ``{server, entry}``, a card step only."""
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.dashboard.handlers.change_cards import HEADER_CARD_ID

    operation = "template_mcp_addition_add"
    denied = await _owner_only(request, operation)
    if denied is not None:
        return denied
    card_id = (request.headers.get(HEADER_CARD_ID) or "").strip()
    if not card_id:
        # The entry is only ever one a card showed: the gateway built it, the
        # owner read it on the card. A bare POST has neither.
        return _deny(403, "card_required", "an agent's servers are added from a change card")
    try:
        body = await request.json()
    except Exception:
        return _deny(400, "invalid_json", "invalid JSON body")
    if not isinstance(body, dict) or set(body) != {"server", "entry"}:
        return _deny(400, "invalid_body", "body is {server, entry}")
    template = request.match_info["name"]
    try:
        added = await asyncio.to_thread(
            additions.add_addition,
            template,
            body["server"],
            body["entry"],
            approved_by=str(request.get("user") or "owner"),
            card_id=card_id,
        )
    except additions.AdditionError as exc:
        return _deny(400, "addition_refused", str(exc))
    except (OSError, ValueError):
        logger.warning("template MCP additions record unwritable", exc_info=True)
        return _deny(503, "record_unavailable", "the agent server record could not be written")
    _audit(
        str(request.get("user") or "owner"),
        operation,
        "ok",
        f"{template}:{added.server}",
    )
    applied = await _apply_to_sessions(request)
    return web.json_response(
        {
            "ok": True,
            "template": template,
            "server": added.server,
            "card_id": card_id,
            **applied,
        }
    )


async def api_template_mcp_addition_remove(request: web.Request) -> web.Response:
    """DELETE /api/agents/detail/{name}/mcp-additions/{server}[?card_id=] — remove one addition."""
    from kiro_crew import template_mcp_additions as additions

    operation = "template_mcp_addition_remove"
    denied = await _owner_only(request, operation)
    if denied is not None:
        return denied
    template = request.match_info["name"]
    server = request.match_info["server"]
    card_id = request.query.get("card_id") or None
    try:
        removed = await asyncio.to_thread(
            additions.remove_addition, template, server, card_id=card_id
        )
    except additions.AdditionError as exc:
        return _deny(400, "addition_refused", str(exc))
    except (OSError, ValueError):
        logger.warning("template MCP additions record unwritable", exc_info=True)
        return _deny(503, "record_unavailable", "the agent server record could not be written")
    if not removed:
        return _deny(
            409,
            "not_found",
            "that server is not added to this agent by this card any more",
        )
    _audit(str(request.get("user") or "owner"), operation, "ok", f"{template}:{server}")
    applied = await _apply_to_sessions(request)
    return web.json_response({"ok": True, "template": template, "server": server, **applied})
