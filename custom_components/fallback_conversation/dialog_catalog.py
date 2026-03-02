"""Dialog phrase catalog loading and rebuild helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import glob
import logging
from pathlib import Path
import re
from typing import Any

import yaml

from homeassistant.core import HomeAssistant

from .const import (
    CONF_DIALOG_YAML_PATH,
    CONF_INCLUDE_CONVERSATION_TRIGGER_SCAN,
    DEFAULT_DIALOG_YAML_PATH,
    DEFAULT_INCLUDE_CONVERSATION_TRIGGER_SCAN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_SLOT_RE = re.compile(r"\{[^}]+\}")
_ALT_GROUP_RE = re.compile(r"(\(|\[)([^()\[\]]*\|[^()\[\]]*)(\)|\])")
_MAX_PATTERN_EXPANSIONS = 64


@dataclass(frozen=True)
class _DialogPhraseLoadStats:
    yaml_count: int
    trigger_count: int
    total_count: int


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedupe_phrases(phrases: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for phrase in phrases:
        normalized = _norm(phrase)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _expand_pattern_variants(pattern: str, limit: int = _MAX_PATTERN_EXPANSIONS) -> list[str]:
    text = _norm(_SLOT_RE.sub(" ", pattern))
    if not text:
        return []

    variants = [text]
    while True:
        expanded = False
        next_variants: list[str] = []
        for variant in variants:
            match = _ALT_GROUP_RE.search(variant)
            if not match:
                next_variants.append(_norm(variant))
                continue

            expanded = True
            options = [opt.strip() for opt in match.group(2).split("|") if opt.strip()]
            if not options:
                options = [""]
            for opt in options:
                repl = f"{variant[:match.start()]} {opt} {variant[match.end():]}"
                next_variants.append(_norm(repl))
                if len(next_variants) >= limit:
                    break
            if len(next_variants) >= limit:
                break

        variants = next_variants
        if not expanded or len(variants) >= limit:
            break

    return _dedupe_phrases(variants)


def _collect_conversation_commands(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        if node.get("platform") == "conversation" and "command" in node:
            command = node.get("command")
            if isinstance(command, str):
                out.append(command)
            elif isinstance(command, list):
                out.extend([item for item in command if isinstance(item, str)])
        for value in node.values():
            _collect_conversation_commands(value, out)
        return

    if isinstance(node, list):
        for item in node:
            _collect_conversation_commands(item, out)


def _load_yaml_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed loading YAML from %s", path)
        return None


def _extract_dialog_yaml_phrases(path: str) -> list[str]:
    root = _load_yaml_file(Path(path))
    if not isinstance(root, dict):
        return []
    phrases = root.get("phrases")
    if not isinstance(phrases, list):
        return []
    return [item for item in phrases if isinstance(item, str)]


def _extract_conversation_trigger_phrases() -> list[str]:
    commands: list[str] = []

    for automation_file in (Path("/config/automations.yaml"), Path("/config/automation.yaml")):
        root = _load_yaml_file(automation_file)
        if root is not None:
            _collect_conversation_commands(root, commands)

    blueprint_files = glob.glob("/config/blueprints/automation/**/*.yaml", recursive=True)
    for path_str in blueprint_files:
        root = _load_yaml_file(Path(path_str))
        if root is not None:
            _collect_conversation_commands(root, commands)

    expanded: list[str] = []
    for command in commands:
        expanded.extend(_expand_pattern_variants(command))

    return _dedupe_phrases(expanded)


async def _async_load_dialog_phrases_with_stats(
    hass: HomeAssistant, options: dict[str, Any] | None
) -> tuple[list[str], _DialogPhraseLoadStats]:
    options = options or {}
    yaml_path = str(options.get(CONF_DIALOG_YAML_PATH, DEFAULT_DIALOG_YAML_PATH))
    include_trigger_scan = bool(
        options.get(
            CONF_INCLUDE_CONVERSATION_TRIGGER_SCAN,
            DEFAULT_INCLUDE_CONVERSATION_TRIGGER_SCAN,
        )
    )

    yaml_phrases = await hass.async_add_executor_job(_extract_dialog_yaml_phrases, yaml_path)
    trigger_phrases: list[str] = []
    if include_trigger_scan:
        trigger_phrases = await hass.async_add_executor_job(_extract_conversation_trigger_phrases)

    all_phrases = _dedupe_phrases([*yaml_phrases, *trigger_phrases])
    stats = _DialogPhraseLoadStats(
        yaml_count=len(_dedupe_phrases(yaml_phrases)),
        trigger_count=len(trigger_phrases),
        total_count=len(all_phrases),
    )
    return all_phrases, stats


async def async_load_dialog_phrases(
    hass: HomeAssistant, options: dict[str, Any] | None
) -> list[str]:
    """Load dialog phrases from configured sources."""
    phrases, stats = await _async_load_dialog_phrases_with_stats(hass, options)
    _LOGGER.debug(
        "Dialog catalog loaded: total=%d yaml=%d conversation_triggers=%d",
        stats.total_count,
        stats.yaml_count,
        stats.trigger_count,
    )
    return phrases


async def async_rebuild_dialog_phrases(
    hass: HomeAssistant,
    entry_id: str,
    options: dict[str, Any] | None,
) -> list[str]:
    """Rebuild dialog phrase catalog and cache in hass.data."""
    phrases, stats = await _async_load_dialog_phrases_with_stats(hass, options)
    domain_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
    domain_data["dialog_phrases"] = phrases
    domain_data["dialog_phrase_stats"] = {
        "yaml": stats.yaml_count,
        "conversation_triggers": stats.trigger_count,
        "total": stats.total_count,
    }
    _LOGGER.debug(
        "Dialog catalog rebuilt for entry %s: total=%d yaml=%d conversation_triggers=%d",
        entry_id,
        stats.total_count,
        stats.yaml_count,
        stats.trigger_count,
    )
    return phrases
