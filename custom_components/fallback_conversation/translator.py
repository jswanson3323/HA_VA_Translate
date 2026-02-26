# custom_components/fallback_conversation/translator.py
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Iterable, Optional, Tuple

from .catalog import EntityCatalogItem

import logging

_LOGGER = logging.getLogger(__name__)

# --- tuning ---
MIN_SCORE = 0.88
MIN_MARGIN = 0.06
AREA_MIN_SCORE = 0.72

ALLOW_DOMAINS = {
    "light",
    "switch",
    "fan",
    "cover",
    "climate",
    "script",
    "scene",
    "input_boolean",
    "lock",
}

# Apply only for command-shaped phrases
CONFUSION_MAP = [
    (r"\bgrape room\b", "great room"),
    (r"\bline\b", "light"),
    (r"\blife\b", "light"),
]


@dataclass(frozen=True)
class ActionPlan:
    domain: str
    service: str
    entity_id: str
    value: Optional[float] = None
    normalized_text: str = ""
    match_score: float = 0.0


@dataclass(frozen=True)
class TranslateResult:
    handled: bool
    plan: Optional[ActionPlan] = None
    reason: str = ""
    normalized_text: str = ""


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _looks_like_command(s: str) -> bool:
    return bool(re.match(r"^(turn|switch|toggle|set|increase|decrease)\b", s))


def _apply_confusions(s: str) -> str:
    for pattern, repl in CONFUSION_MAP:
        s = re.sub(pattern, repl, s)
    return s


def _seq(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_set(s: str) -> set[str]:
    return {t for t in s.split() if t}


def _token_score(a: str, b: str) -> float:
    a_set = _token_set(a)
    b_set = _token_set(b)
    if not a_set or not b_set:
        return 0.0
    jacc = len(a_set & b_set) / max(1, len(a_set | b_set))
    seq = _seq(a, b)
    return 0.55 * seq + 0.45 * jacc


def _make_candidates(item: EntityCatalogItem) -> list[str]:
    name = _norm(item.name)
    cands = {name}

    if item.area_name:
        area = _norm(item.area_name)
        # if friendly name begins with area, allow "area-less"
        if name.startswith(area + " "):
            cands.add(name[len(area) + 1 :].strip())
        # allow "area + name" too
        cands.add(f"{area} {name}".strip())

    if item.device_name:
        dev = _norm(item.device_name)
        cands.add(dev)
        if item.area_name:
            cands.add(f"{_norm(item.area_name)} {dev}".strip())

    return sorted(cands)


def _best_area_match(target: str, catalog: Iterable[EntityCatalogItem]) -> Optional[str]:
    """Return best fuzzy-matched area name if strong enough."""
    target_n = _norm(target)

    # Remove common device words before area matching
    device_words = {"light", "fan", "switch", "cover", "lock", "thermostat", "scene"}
    tokens = [t for t in target_n.split() if t not in device_words]
    target_n = " ".join(tokens)

    areas = { _norm(i.area_name) for i in catalog if i.area_name }

    best_area = None
    best_score = 0.0

    for area in areas:
        score = _token_score(target_n, area)
        _LOGGER.warning("[AREA SCORE] target='%s' area='%s' score=%.3f", target_n, area, score)
        if score > best_score:
            best_score = score
            best_area = area

    if best_area:
        _LOGGER.warning("[AREA BEST] target='%s' best_area='%s' best_score=%.3f", target_n, best_area, best_score)

    if best_score >= AREA_MIN_SCORE:
        return best_area

    return None


def _resolve_entity(
    target: str,
    catalog: Iterable[EntityCatalogItem],
    satellite_area: Optional[str] = None,
) -> Tuple[Optional[str], float]:
    target_n = _norm(target)
    sat_area_n = _norm(satellite_area) if satellite_area else None

    # Try resolving area first (improves "break room" vs "great room")
    matched_area = _best_area_match(target, catalog)

    best = None  # (score, item)
    second = None

    for item in catalog:
        if item.domain not in ALLOW_DOMAINS:
            continue

        # If we detected a likely area, prefer entities in that area
        if matched_area and item.area_name:
            if _norm(item.area_name) != matched_area:
                continue

        for cand in _make_candidates(item):
            score = _token_score(target_n, cand)
            _LOGGER.warning(
                "[ENTITY SCORE] target='%s' cand='%s' entity='%s' score=%.3f",
                target_n,
                cand,
                item.entity_id,
                score,
            )

            # tiny tie-break bump if satellite area matches entity area
            if sat_area_n and item.area_name and _norm(item.area_name) == sat_area_n:
                score = min(1.0, score + 0.015)

            if best is None or score > best[0]:
                second = best
                best = (score, item)
            elif second is None or score > second[0]:
                second = (score, item)

    if best is None:
        return None, 0.0

    best_score = best[0]
    second_score = second[0] if second else 0.0

    if best_score < MIN_SCORE:
        return None, best_score
    if (best_score - second_score) < MIN_MARGIN:
        return None, best_score

    return best[1].entity_id, best_score


def _parse_action(s: str):
    # on/off/toggle
    m = re.match(r"^(turn on|turn off|switch on|switch off|toggle)\s+(the\s+)?(.+)$", s)
    if m:
        verb = m.group(1)
        target = m.group(3)
        if verb in ("turn on", "switch on"):
            return ("on", target)
        if verb in ("turn off", "switch off"):
            return ("off", target)
        return ("toggle", target)

    # set X to N
    m = re.match(r"^set\s+(.+?)\s+to\s+([0-9]+(\.[0-9]+)?)\b", s)
    if m:
        return ("set", m.group(1), float(m.group(2)))

    return None


def translate_to_action(
    text: str,
    catalog: Iterable[EntityCatalogItem],
    satellite_area: Optional[str] = None,
) -> TranslateResult:
    if not text:
        return TranslateResult(handled=False, reason="no_text")

    normalized = _norm(text)

    if not _looks_like_command(normalized):
        return TranslateResult(handled=False, reason="not_command", normalized_text=normalized)

    normalized = _apply_confusions(normalized)

    parsed = _parse_action(normalized)
    if not parsed:
        return TranslateResult(handled=False, reason="unparsed_command", normalized_text=normalized)

    kind = parsed[0]

    if kind in ("on", "off", "toggle"):
        target = parsed[1]
        entity_id, score = _resolve_entity(target, catalog, satellite_area=satellite_area)
        if not entity_id:
            return TranslateResult(handled=False, reason="no_entity_match", normalized_text=normalized)

        domain = entity_id.split(".", 1)[0]
        if kind == "toggle":
            return TranslateResult(
                handled=True,
                plan=ActionPlan(
                    domain="homeassistant",
                    service="toggle",
                    entity_id=entity_id,
                    normalized_text=normalized,
                    match_score=score,
                ),
                normalized_text=normalized,
            )

        return TranslateResult(
            handled=True,
            plan=ActionPlan(
                domain=domain,
                service="turn_on" if kind == "on" else "turn_off",
                entity_id=entity_id,
                normalized_text=normalized,
                match_score=score,
            ),
            normalized_text=normalized,
        )

    if kind == "set":
        _, target, value = parsed
        entity_id, score = _resolve_entity(target, catalog, satellite_area=satellite_area)
        if not entity_id:
            return TranslateResult(handled=False, reason="no_entity_match", normalized_text=normalized)

        domain = entity_id.split(".", 1)[0]
        if domain != "climate":
            return TranslateResult(handled=False, reason="set_not_climate", normalized_text=normalized)

        return TranslateResult(
            handled=True,
            plan=ActionPlan(
                domain="climate",
                service="set_temperature",
                entity_id=entity_id,
                value=value,
                normalized_text=normalized,
                match_score=score,
            ),
            normalized_text=normalized,
        )

    return TranslateResult(handled=False, reason="unsupported", normalized_text=normalized)