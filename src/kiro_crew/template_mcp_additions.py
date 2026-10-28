"""Per-template MCP server additions, kept OUTSIDE every agent spec file.

An agent template's ``mcpServers`` map is rewritten by its installer: the conductor
installers rebuild theirs to exactly the Crew servers on every pass, the worker
mirrors the default agent, and a third-party custom agent may be rewritten by its
own installer. A server the user approved for a template therefore cannot live in
the spec file -- the next rebuild would wipe it, or a hand edit and an approved
grant would look the same. This module is the record that survives: one JSON file
under the data home, keyed by template name, each entry a GATEWAY-RESOLVED server
entry plus who approved it, when, and on which change card.

What the record never carries is trust. An entry holds how to launch or reach a
server and nothing about which of its tools run without asking: ``autoApprove``,
``allowedTools`` and every other key outside :data:`ENTRY_KEYS` are refused on
write and dropped again on merge, so a hand-edited file cannot smuggle a grant in
either. The merged server's tools prompt like any other until the user grants them
through the existing paths, which still pass ``_filter_auto_approve``.

:func:`merge_into` is the one function every template's ``mcpServers`` passes
through on its way to a session (the conductor installers, the worker mirror, the
default agent, and the spawn of a third-party custom agent). It only ADDS: a server
name the template already carries is left exactly as the template has it.

The file is owner-only, every write is atomic, and every read-modify-write holds
an in-process lock plus a cross-process advisory lock on a sidecar file.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import hashlib
import json
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger(__name__)

#: The record's DIRECTORY under the data home: a sealed leaf (read-only to every
#: sandboxed process and denied to the file-tool gate), because a sandboxed writer
#: could otherwise record a server it chose and have the next rebuild launch it.
#: Every writer runs in the gateway or the CLI, outside the sandbox.
RECORD_DIR = "template-mcp-additions"
#: The record file inside :data:`RECORD_DIR`.
RECORD_FILENAME = "additions.json"
RECORD_VERSION = 1
RECORD_MAX_BYTES = 1024 * 1024

#: The only keys an addition's server entry may carry: how to launch or reach the
#: server. Nothing here grants a tool. ``autoApprove`` / ``allowedTools`` /
#: ``disabledTools`` and anything else are refused on write and dropped on merge.
ENTRY_KEYS: frozenset[str] = frozenset(
    {"command", "args", "env", "url", "headers", "type", "timeout"}
)

#: Keys whose presence on an entry is a trust grant. Named separately only so the
#: refusal can say why; :data:`ENTRY_KEYS` is the rule.
TRUST_KEYS: frozenset[str] = frozenset(
    {"autoApprove", "alwaysAllow", "allowedTools", "tools", "toolsSettings", "trust"}
)

_TEMPLATE_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9]|[A-Za-z0-9])\Z")
_SERVER_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9]|[A-Za-z0-9])\Z")
_TEXT_MAX = 256
_ARG_MAX = 4096
_ARGS_MAX = 128
_MAP_MAX = 64

_lock = threading.RLock()

#: The default agent's template name.
DEFAULT_TEMPLATE = "kirocrew"
#: The worker's template name. Its additions are its OWN entry, merged onto the
#: mirror of the default agent, so a worker grant never reaches default sessions.
WORKER_TEMPLATE = "kirocrew-worker"
#: Templates whose installer rebuilds their spec and merges the record into it.
#: Every other template (a third-party or other custom agent) is merged at spawn
#: instead, never into its spec file (:func:`merge_at_spawn`).
INSTALLER_MERGED_TEMPLATES: frozenset[str] = frozenset(
    {
        DEFAULT_TEMPLATE,
        WORKER_TEMPLATE,
        "kirocrew-conductor",
        "kirocrew-ledger-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
    }
)
#: A deprecated alias emits its successor's spec, so it reads its successor's record.
_TEMPLATE_ALIASES: dict[str, str] = {"kirocrew-ledger-conductor": "kirocrew-conductor"}


class AdditionError(ValueError):
    """A refused addition: the message is a short machine-readable reason."""


@dataclass(frozen=True)
class McpAddition:
    """One approved server on one template."""

    template: str
    server: str
    entry: Mapping[str, Any] = field(repr=False)
    approved_by: str
    approved_at: str
    card_id: str

    def to_record(self) -> dict[str, Any]:
        return {
            "entry": copy.deepcopy(dict(self.entry)),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "card_id": self.card_id,
        }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def read_additions(template: str) -> dict[str, McpAddition]:
    """The additions recorded for *template*, server name -> addition.

    Lenient: an absent, unreadable or malformed record reads as no additions, and
    an individual entry that fails validation is skipped (and logged), so a damaged
    file degrades to the template as shipped rather than breaking a spawn.
    """
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        return {}
    template = _TEMPLATE_ALIASES.get(template, template)
    templates = _read().get("templates", {})
    if not isinstance(templates, dict):
        return {}
    return _parse_template(template, templates.get(template))


def list_additions() -> dict[str, dict[str, McpAddition]]:
    """Every template's additions, template -> server -> addition (lenient)."""
    out: dict[str, dict[str, McpAddition]] = {}
    templates = _read().get("templates", {})
    if not isinstance(templates, dict):
        return out
    for template in sorted(templates):
        if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
            continue
        parsed = _parse_template(template, templates[template])
        if parsed:
            out[template] = parsed
    return out


def add_addition(
    template: str,
    server: str,
    entry: Mapping[str, Any],
    *,
    approved_by: str,
    card_id: str,
    approved_at: str | None = None,
) -> McpAddition:
    """Record *server* on *template* and return the stored addition.

    *entry* must be the gateway-resolved server entry (never an agent-supplied
    launch line) and carry only :data:`ENTRY_KEYS`; a trust key is refused with
    ``AdditionError("trust_key:<key>")``. Recording a server the template already
    has an addition for replaces that addition (the newer approval wins).
    Raises :class:`AdditionError` on a refused input and ``OSError`` /
    ``ValueError`` when an existing record cannot be read (it is never overwritten
    blind).
    """
    _check_template(template)
    _check_server(server)
    clean = validate_entry(entry)
    who = _check_text(approved_by, "approved_by")
    card = _check_text(card_id, "card_id")
    when = approved_at if approved_at is not None else _now()
    _check_text(when, "approved_at")
    addition = McpAddition(
        template=template,
        server=server,
        entry=clean,
        approved_by=who,
        approved_at=when,
        card_id=card,
    )
    with _locked():
        data = _read(strict=True)
        templates = data.setdefault("templates", {})
        if not isinstance(templates, dict):
            raise ValueError("template_mcp_additions_invalid")
        bucket = templates.setdefault(template, {})
        if not isinstance(bucket, dict):
            raise ValueError("template_mcp_additions_invalid")
        bucket[server] = addition.to_record()
        _write(data)
    return addition


def remove_addition(template: str, server: str, *, card_id: str | None = None) -> bool:
    """Remove *server* from *template*'s additions; whether anything was removed.

    With *card_id*, remove only when the stored addition came from that card, so a
    stale Undo cannot take away a later approval of the same server.
    """
    _check_template(template)
    _check_server(server)
    with _locked():
        data = _read(strict=True)
        templates = data.get("templates")
        if not isinstance(templates, dict):
            return False
        bucket = templates.get(template)
        if not isinstance(bucket, dict) or server not in bucket:
            return False
        stored = bucket[server]
        if card_id is not None and (
            not isinstance(stored, dict) or stored.get("card_id") != card_id
        ):
            return False
        del bucket[server]
        if not bucket:
            del templates[template]
        _write(data)
    return True


def merge_into(template: str, servers: Mapping[str, Any] | None) -> dict[str, Any]:
    """*servers* plus *template*'s recorded additions, as a new map.

    ADD-only: a server name *servers* already carries keeps the template's own
    entry. Each added entry is rebuilt from :data:`ENTRY_KEYS` alone, so no trust
    key reaches a spec even from a hand-edited record. *servers* is not mutated.
    """
    merged: dict[str, Any] = dict(servers or {})
    for name, addition in read_additions(template).items():
        if name in merged:
            continue
        merged[name] = copy.deepcopy(dict(addition.entry))
    return merged


def merge_into_spec(template: str, spec: dict[str, Any]) -> list[str]:
    """Merge *template*'s additions into *spec* IN PLACE; the names it added.

    :func:`merge_into` on ``spec["mcpServers"]``, plus the ``@server`` reference in
    ``tools`` that makes the server's tools reachable at all (kiro-cli mounts a
    declared server's tools only when ``tools`` names it). That reference is not a
    grant: ``allowedTools`` and ``autoApprove`` are never touched, so every call
    still reaches the PreToolUse gate. A ``tools`` that is absent or not a list is
    left alone -- an absent one is the harness default and a malformed one is the
    spec owner's to fix, not this merge's to widen.
    """
    servers = spec.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return []
    merged = merge_into(template, servers)
    added = [name for name in merged if name not in (servers or {})]
    if not added:
        return []
    spec["mcpServers"] = merged
    tools = spec.get("tools")
    if isinstance(tools, list):
        for name in added:
            ref = f"@{name}"
            if ref not in tools:
                tools.append(ref)
    return added


@dataclass(frozen=True)
class AdditionSupport:
    """Whether an addition to *template* reaches its sessions on *backend*.

    ``reason`` is empty when ``supported``; otherwise it is the short sentence a
    card refusal shows. ``supported`` says nothing about a RUNNING session: whether
    the change reaches one without a reset is the existing live gate
    (``mcp_hot_reload.live_sessions_apply_mcp_live``), asked by the caller over the
    live providers.
    """

    supported: bool
    reason: str = ""


#: The refusal for a template Crew does not own on a backend that loads it off disk.
UNSUPPORTED_REASON = (
    "This agent's definition is not managed by Kiro Crew, and on this harness the "
    "agent is loaded from its own file. Kiro Crew never writes into a definition it "
    "does not own, so a server cannot be added to it here."
)


def addition_support(template: str, backend: str) -> AdditionSupport:
    """Whether an addition to *template* reaches its sessions on *backend*.

    A template Crew owns (:data:`INSTALLER_MERGED_TEMPLATES`) is merged by its
    installer into the spec every backend reads, so it is supported everywhere.
    Any other template is merged at spawn, which only the backends in
    ``ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN`` take (a positive membership test,
    per harness parity); on the rest -- kiro-cli first among them -- the answer is
    unsupported with :data:`UNSUPPORTED_REASON`.
    """
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        return AdditionSupport(False, "That agent name is not valid.")
    if template in INSTALLER_MERGED_TEMPLATES:
        return AdditionSupport(True)
    from kiro_crew.acp_backends import ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN

    if backend in ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN:
        return AdditionSupport(True)
    return AdditionSupport(False, UNSUPPORTED_REASON)


def merge_at_spawn(template: str | None, spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """*spec* as a session of a template Crew does NOT own should read it at spawn.

    Returns a NEW dict with the additions merged (:func:`merge_into_spec`) for a
    template outside :data:`INSTALLER_MERGED_TEMPLATES`; returns *spec* itself for a
    Crew-owned template (its installer already merged them into the file), for
    ``None``, and when there is nothing to add. Never writes any file. Called only
    from the spawn reads of the backends in ``ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN``.
    """
    if not template or not isinstance(spec, dict) or template in INSTALLER_MERGED_TEMPLATES:
        return spec
    if not read_additions(template):
        return spec
    out = copy.deepcopy(spec)
    merge_into_spec(template, out)
    return out


def addition_reach(template: str) -> tuple[str, ...]:
    """Every template one addition to *template* reaches, *template* first.

    Two tables decide it, and a test pins both to their owners so this cannot drift:
    a deprecated alias emits its successor's spec, so an addition to either reaches
    both (:data:`_TEMPLATE_ALIASES`, ``conductor_agents.DEPRECATED_AGENT_SPECS``); and
    the worker mirrors the default agent's whole ``mcpServers`` map
    (``worker_agent._WORKER_MIRRORED_SHAPES``), so an addition to the default agent
    reaches the worker too. Nothing else copies a template's servers: the heartbeat
    copies a fixed core list, and a worker addition reaches the worker alone.
    """
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        return ()
    canonical = _TEMPLATE_ALIASES.get(template, template)
    reach = [template]
    for name in (
        canonical,
        *sorted(a for a, c in _TEMPLATE_ALIASES.items() if c == canonical),
    ):
        if name not in reach:
            reach.append(name)
    if canonical == DEFAULT_TEMPLATE:
        reach.append(WORKER_TEMPLATE)
    return tuple(reach)


def additions_digest(template: str) -> str:
    """A content digest of *template*'s additions, for a freshness check to compare.

    Covers the entries only, not the provenance: re-approving the same entry on a
    new card launches the same server, so it is not a different generation.
    """
    payload = json.dumps(
        {name: dict(add.entry) for name, add in read_additions(template).items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mirror_marker(agent: str) -> str:
    """The worker mirror's additions marker: its digest, or ``""`` when *agent* is not
    the worker or it has no additions (so a mirror's recorded generation is exactly
    what it was before this record existed)."""
    if agent != WORKER_TEMPLATE or not read_additions(agent):
        return ""
    return additions_digest(agent)


def mirror_stat_value(identity: str | None, marker: str) -> str | None:
    """The fast-path identity a worker mirror records: the default spec's file
    identity, plus the additions marker it merged when there is one."""
    if identity is None:
        return None
    return f"{identity}+{marker}" if marker else identity


def project_into_rendered_spec(
    template: str, spec: dict[str, Any], *, claimed: frozenset[str] = frozenset()
) -> list[str]:
    """Bring a spec that is rebuilt FROM ITS OWN FILE in line with the record.

    The conductor installers and the worker mirror rebuild their ``mcpServers``
    from scratch, so :func:`merge_into_spec` is all they need. The default agent's
    rebuild starts from the file on disk instead, so a removed addition would
    survive there as if the user had written it. This tracks what it projected (a
    ``projected`` map in the record, never in the spec) and, on the next pass,
    drops a projected name the record does not hold -- the entry and its
    ``@server`` reference -- unless another source in *claimed* now declares it.
    Grants the user made on that server separately are left to the rebuild's own
    dangling-reference pass. Returns the names this pass added.
    """
    current = read_additions(template)
    with _locked():
        try:
            data = _read(strict=True)
        except (OSError, ValueError):
            # Unreadable record: project nothing new and remove nothing. Removal
            # needs the bookkeeping, and guessing it from the spec would drop
            # entries the user wrote.
            logger.warning("Template MCP additions record unreadable; projection skipped")
            return []
        projected = data.setdefault("projected", {})
        if not isinstance(projected, dict):
            projected = data["projected"] = {}
        before = projected.get(template)
        before_names = (
            {n for n in before if isinstance(n, str)} if isinstance(before, list) else set()
        )
        servers = spec.get("mcpServers")
        servers = dict(servers) if isinstance(servers, dict) else {}
        tools = spec.get("tools")
        for name in sorted(before_names - set(current) - set(claimed)):
            servers.pop(name, None)
            if isinstance(tools, list):
                spec["tools"] = tools = [t for t in tools if t != f"@{name}"]
        spec["mcpServers"] = servers
        added = merge_into_spec(template, spec)
        # A name stays projected while the record holds it and the spec carries
        # the projected copy (added now, or added on an earlier pass).
        now = sorted(
            name
            for name in current
            if name in added or (name in before_names and name in spec["mcpServers"])
        )
        if now != sorted(before_names):
            if now:
                projected[template] = now
            else:
                projected.pop(template, None)
            _write(data)
    return added


def validate_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return a clean copy of a server entry, or raise :class:`AdditionError`."""
    if not isinstance(entry, Mapping):
        raise AdditionError("entry_not_object")
    for key in entry:
        if key in TRUST_KEYS:
            raise AdditionError(f"trust_key:{key}")
        if key not in ENTRY_KEYS:
            raise AdditionError(f"unknown_key:{key}")
    clean: dict[str, Any] = {}
    command = entry.get("command")
    url = entry.get("url")
    if (command is None) == (url is None):
        raise AdditionError("entry_needs_command_or_url")
    if command is not None:
        clean["command"] = _check_text(command, "command", limit=_ARG_MAX)
        args = entry.get("args", [])
        if not isinstance(args, list) or len(args) > _ARGS_MAX:
            raise AdditionError("args_invalid")
        clean["args"] = [_check_text(a, "args", limit=_ARG_MAX, allow_empty=True) for a in args]
    else:
        if "args" in entry:
            raise AdditionError("args_without_command")
        clean["url"] = _check_text(url, "url", limit=_ARG_MAX)
    for key in ("env", "headers"):
        if key in entry:
            clean[key] = _check_str_map(entry[key], key)
    if "type" in entry:
        clean["type"] = _check_text(entry["type"], "type")
    if "timeout" in entry:
        timeout = entry["timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 0 < timeout <= 3_600_000
        ):
            raise AdditionError("timeout_invalid")
        clean["timeout"] = timeout
    return clean


def record_path() -> Path:
    """Where the record lives (resolved per call so ``KIROCREW_HOME`` is honoured)."""
    return config_dir() / RECORD_DIR / RECORD_FILENAME


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _parse_template(template: str, bucket: Any) -> dict[str, McpAddition]:
    out: dict[str, McpAddition] = {}
    if not isinstance(bucket, dict):
        return out
    for server in sorted(bucket):
        stored = bucket[server]
        try:
            _check_server(server)
            if not isinstance(stored, dict):
                raise AdditionError("addition_not_object")
            out[server] = McpAddition(
                template=template,
                server=server,
                entry=validate_entry(stored.get("entry")),
                approved_by=_check_text(stored.get("approved_by"), "approved_by"),
                approved_at=_check_text(stored.get("approved_at"), "approved_at"),
                card_id=_check_text(stored.get("card_id"), "card_id"),
            )
        except AdditionError as exc:
            logger.warning(
                "Skipping unusable MCP addition %r on template %r: %s",
                server,
                template,
                exc,
            )
    return out


def _check_template(template: object) -> None:
    if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
        raise AdditionError("template_invalid")


def _check_server(server: object) -> None:
    if not isinstance(server, str) or not _SERVER_RE.fullmatch(server):
        raise AdditionError("server_invalid")


def _check_text(
    value: object, what: str, *, limit: int = _TEXT_MAX, allow_empty: bool = False
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > limit:
        raise AdditionError(f"{what}_invalid")
    if "\x00" in value:
        raise AdditionError(f"{what}_invalid")
    return value


def _check_str_map(value: object, what: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > _MAP_MAX:
        raise AdditionError(f"{what}_invalid")
    out: dict[str, str] = {}
    for key, item in value.items():
        out[_check_text(key, what)] = _check_text(item, what, limit=_ARG_MAX, allow_empty=True)
    return out


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """In-process lock plus a cross-process advisory lock on a sidecar file."""
    with _lock:
        from kiro_crew.platform_compat import file_lock

        lock_path = record_path().with_suffix(".json.lock")
        # ``0700`` under any umask: the record directory is owner-only, and this is
        # usually the first thing to create it.
        mkdirs_owner_only(lock_path.parent)
        fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("template_mcp_additions_lock_invalid")
            with file_lock(fd, exclusive=True, wait=True):
                yield
        finally:
            os.close(fd)


def _read(*, strict: bool = False) -> dict[str, Any]:
    """Load the record. ``strict`` (mutators) raises on a present-but-unreadable file."""
    path = record_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("template_mcp_additions_invalid")
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > RECORD_MAX_BYTES:
                raise ValueError("template_mcp_additions_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(RECORD_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > RECORD_MAX_BYTES:
            raise ValueError("template_mcp_additions_too_large")
        data = json.loads(raw)
    except FileNotFoundError:
        return {"version": RECORD_VERSION, "templates": {}}
    except (OSError, ValueError):
        if strict:
            raise
        logger.warning("Template MCP additions record unreadable: %s", path, exc_info=True)
        return {"version": RECORD_VERSION, "templates": {}}
    if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
        if strict:
            raise ValueError("template_mcp_additions_invalid")
        return {"version": RECORD_VERSION, "templates": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    data["version"] = RECORD_VERSION
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > RECORD_MAX_BYTES:
        raise ValueError("template_mcp_additions_too_large")
    atomic_write(record_path(), payload, restrict_to_owner=True)
