"""Elephant-Goldfish Model for Hermes Agent.

Registers eight workflow skills, seven tools, nine slash commands and one CLI
command family. The engine lives in :mod:`egm`; this module is wiring only.

Upstream: https://github.com/google/elephant-goldfish-model (Apache-2.0).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_ROOT = Path(__file__).resolve().parent

#: Skill name -> the 60-character description shown in the prompt index.
SKILLS: dict[str, str] = {
    "egm-brainstorm": "Turn a half-formed idea into a reviewed concept brief.",
    "egm-draft": "Draft a design doc section by section, with approval gates.",
    "egm-review": "Five-stage review of a design by fresh-context reviewers.",
    "egm-review-loop": "Iterate Goldfish review rounds until findings converge.",
    "egm-resolve": "Triage review findings and apply approved fixes.",
    "egm-bootstrap": "Generate README contracts across a codebase tree.",
    "egm-lite": "Fast EGM loop for small changes: critique, plan, review.",
    "egm-drift-report": "Find where checked-in docs diverge from the code.",
}


def register(ctx) -> None:
    """Entry point called by the Hermes plugin loader."""
    _register_skills(ctx)
    _register_tools(ctx)
    _register_commands(ctx)
    _register_cli(ctx)
    logger.info("EGM registered: %d skills, tools=%s", len(SKILLS),
                ",".join(_tool_names()))


def _register_skills(ctx) -> None:
    for name, description in SKILLS.items():
        path = PLUGIN_ROOT / "skills" / name / "SKILL.md"
        if not path.exists():
            logger.warning("EGM skill %s is missing its SKILL.md at %s", name, path)
            continue
        try:
            ctx.register_skill(
                name, path, description,
                {"name": name, "description": description, "license": "Apache-2.0"},
            )
        except Exception as exc:  # noqa: BLE001 - one bad skill must not disable the rest
            logger.warning("EGM skill %s could not be registered: %s", name, exc)


def _register_tools(ctx) -> None:
    from .egm import tools as tool_module
    for registration in tool_module.iter_tool_registrations(ctx):
        try:
            ctx.register_tool(**registration)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EGM tool %s could not be registered: %s",
                           registration.get("name"), exc)


def _register_commands(ctx) -> None:
    from .egm import slash
    try:
        ctx.register_command(
            "egm", slash.make_help(ctx),
            description="Elephant-Goldfish Model: workflows, inventory and quick checks",
            args_hint="[brainstorm|draft|review|bootstrap|lite|drift-report|lint|plan|status|personas]",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("EGM /egm command could not be registered: %s", exc)
    for alias, skill in slash.ALIASES.items():
        try:
            ctx.register_command(
                alias, slash.make_alias(ctx, skill),
                description=SKILLS.get(skill, "EGM workflow"),
                args_hint=slash._USAGE.get(skill, ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("EGM /%s command could not be registered: %s", alias, exc)


def _register_cli(ctx) -> None:
    from .egm import cli
    try:
        ctx.register_cli_command(
            name="egm",
            help="Elephant-Goldfish Model: inventory, plan, lint, drift, bootstrap",
            setup_fn=cli.setup_parser,
            handler_fn=cli.handle,
            description=("Inspect the EGM plugin and run its deterministic operations. "
                         "Reviews themselves run in a chat session because they need "
                         "fresh-context reviewer children."),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("EGM CLI command could not be registered: %s", exc)


def _tool_names() -> tuple[str, ...]:
    try:
        from .egm.tools import tool_names
        return tool_names()
    except Exception:  # noqa: BLE001
        return ()
