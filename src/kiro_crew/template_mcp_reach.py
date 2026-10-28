"""Which agent templates one MCP addition reaches, and where a request records it.

An addition is recorded on one template (:mod:`kiro_crew.template_mcp_additions`),
but its sessions are not only that template's: the worker mirrors the default
agent, so a server added to ``kirocrew`` also reaches every ``kirocrew-worker``
session, and the deprecated ledger conductor reads the goal conductor's record. A
``template.mcp_server`` card lists every template (and open session) the addition
reaches, so the user approves the real scope. The record module owns both rules;
this is the one place the request side reads them.
"""

from __future__ import annotations

from kiro_crew import template_mcp_additions as _additions


def addition_reach(template: str) -> tuple[str, ...]:
    """The template names an addition recorded on *template* reaches, *template* first."""
    return _additions.addition_reach(template)


def record_template(template: str) -> str:
    """The template name *template*'s additions are recorded under (an alias reads its target).

    ``read_additions`` reads an alias's target, but ``add_addition`` writes the name
    it is given, so a request from an alias session records under the target.
    """
    return _additions._TEMPLATE_ALIASES.get(template, template)
