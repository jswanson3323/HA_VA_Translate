"""Fallback Conversation Agent."""

from __future__ import annotations

import logging

from homeassistant.components import assist_pipeline, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from home_assistant_intents import get_languages

from .catalog import async_get_exposed_catalog
from .const import (
    CONF_DIALOG_BYPASS_MIN_SCORE,
    CONF_DEBUG_LEVEL,
    CONF_ENABLE_DIALOG_BYPASS,
    CONF_FALLBACK_AGENT,
    DEFAULT_DIALOG_BYPASS_MIN_SCORE,
    DEFAULT_ENABLE_DIALOG_BYPASS,
    CONF_PRIMARY_AGENT,
    DEBUG_LEVEL_LOW_DEBUG,
    DEBUG_LEVEL_NO_DEBUG,
    DEBUG_LEVEL_VERBOSE_DEBUG,
    DOMAIN,
    STRANGE_ERROR_RESPONSES,
)
from .translator import ActionPlan, translate_to_action

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _action_speech_from_plan(plan: ActionPlan) -> str:
    entity = plan.entity_id.replace("_", " ")
    if "." in entity:
        entity = entity.split(".", 1)[1]

    if plan.service == "turn_on":
        return f"Turned on {entity}."
    if plan.service == "turn_off":
        return f"Turned off {entity}."
    if plan.service == "toggle":
        return f"Toggled {entity}."
    if plan.service == "set_temperature" and plan.value is not None:
        temp = int(plan.value) if float(plan.value).is_integer() else plan.value
        return f"Set {entity} to {temp} degrees."

    return f"Ran {plan.service} on {entity}."


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> bool:
    """Set up Fallback Conversation from a config entry."""
    agent = FallbackConversationAgent(hass, entry)
    async_add_entities([agent])
    return True


class FallbackConversationAgent(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Fallback Conversation Agent."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self.last_used_agent: str | None = None
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id
        self._attr_supported_features = conversation.ConversationEntityFeature.CONTROL
        self.in_context_examples = None

    # --- agent resolution helpers ---
    _HOME_ASSISTANT_ALIASES = {
        "homeassistant",
        "home_assistant",
        "conversation.homeassistant",
        "conversation.home_assistant",
        "home assistant",
    }

    def _is_home_assistant_ref(self, agent_ref: str | None) -> bool:
        if not agent_ref:
            return False
        return agent_ref.strip().lower() in self._HOME_ASSISTANT_ALIASES

    def _get_default_home_assistant_agent(self):
        """Best-effort lookup for the built-in Home Assistant conversation agent."""
        # 1) Some HA versions expose helper on the conversation module
        for attr in ("async_get_default_agent", "get_default_agent"):
            fn = getattr(conversation, attr, None)
            if callable(fn):
                try:
                    return fn(self.hass)  # type: ignore[misc]
                except TypeError:
                    # maybe async
                    pass

        # 2) Try conversation.default_agent module helpers
        try:
            from homeassistant.components.conversation import default_agent as conv_default  # type: ignore
            for attr in ("async_get_default_agent", "get_default_agent"):
                fn = getattr(conv_default, attr, None)
                if callable(fn):
                    try:
                        return fn(self.hass)  # type: ignore[misc]
                    except TypeError:
                        pass
        except Exception:  # pragma: no cover
            pass

        # 3) Try hass.data conventional locations
        domain_data = self.hass.data.get(conversation.DOMAIN)
        if isinstance(domain_data, dict):
            for key in ("default_agent", "agent", "default"):
                if key in domain_data:
                    return domain_data[key]

        # 4) Try DATA_* constants
        for const_name in ("DATA_DEFAULT_AGENT", "DATA_AGENT", "DATA_DEFAULT"):
            key = getattr(conversation, const_name, None)
            if key and key in self.hass.data:
                return self.hass.data[key]

        return None

    def _build_agent_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (ref->internal_id, ref->display_name)."""
        agent_manager = conversation.get_agent_manager(self.hass)
        id_map: dict[str, str] = {}
        name_map: dict[str, str] = {}

        try:
            infos = agent_manager.async_get_agent_info()
        except Exception:
            infos = []

        for info in infos:
            # info.id is the AgentManager id
            internal_id = info.id
            name_map[internal_id] = info.name
            id_map[internal_id] = internal_id

            try:
                agent = agent_manager.async_get_agent(internal_id)
                reg = getattr(agent, "registry_entry", None)
                ent_id = getattr(reg, "entity_id", None)
                if isinstance(ent_id, str):
                    id_map[ent_id] = internal_id
                    name_map[ent_id] = info.name
            except Exception:
                continue

        # Add built-in Home Assistant agent as a pseudo-entry
        for alias in self._HOME_ASSISTANT_ALIASES:
            id_map[alias] = "__homeassistant__"
            name_map[alias] = "Home Assistant"

        return id_map, name_map

    def _resolve_agent_ref(self, agent_ref: str | None, id_map: dict[str, str]) -> str | None:
        if not agent_ref:
            return None
        ref = agent_ref.strip()
        if not ref:
            return None
        ref_l = ref.lower()

        # direct
        if ref in id_map:
            return id_map[ref]
        if ref_l in id_map:
            return id_map[ref_l]

        # common HA alias coming from older configs / selectors
        if self._is_home_assistant_ref(ref):
            return "__homeassistant__"

        return None


    def _convert_agent_info_to_dict(
        self, agents_info: list[conversation.AgentInfo]
    ) -> dict[str, str]:
        """Map both agent_id and conversation entity_id to display name."""
        agent_manager = conversation.get_agent_manager(self.hass)

        r: dict[str, str] = {}
        for agent_info in agents_info:
            # Canonical agent id
            r[agent_info.id] = agent_info.name

            # Also map the registered conversation entity id, if available
            try:
                agent = agent_manager.async_get_agent(agent_info.id)
            except Exception:  # noqa: BLE001
                agent = None

            if agent is not None and hasattr(agent, "registry_entry"):
                r[agent.registry_entry.entity_id] = agent_info.name

            _LOGGER.debug("agent_id %s has name %s", agent_info.id, agent_info.name)

        return r

    @property
    def supported_languages(self) -> list[str]:
        """Return a list of supported languages."""
        return get_languages()

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

        # Assist pipeline migration helper is HA-version dependent.
        if hasattr(assist_pipeline, "async_migrate_engine"):
            assist_pipeline.async_migrate_engine(
                self.hass,
                "conversation",
                self.entry.entry_id,
                self.entity_id,
            )

        conversation.async_set_agent(self.hass, self.entry, self)
        self.entry.async_on_unload(
            self.entry.add_update_listener(self._async_entry_update_listener)
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_entry_update_listener(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Handle options update."""
        self._attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    
    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""
        agent_manager = conversation.get_agent_manager(self.hass)
        id_map, name_map = self._build_agent_maps()

        debug_level = self.entry.options.get(CONF_DEBUG_LEVEL, DEBUG_LEVEL_NO_DEBUG)

        # Normal HA behavior: keep conversation_id stable for multi-turn flows
        if user_input.conversation_id is None:
            user_input.conversation_id = ulid.ulid()

        # 1) Try translation layer first (best effort; never hard-fail)
        try:
            catalog = await async_get_exposed_catalog(self.hass, assistant="conversation")
            items = await catalog.async_get_items()
            t = translate_to_action(user_input.text, items, satellite_area=user_input.satellite_id)
            if t.handled and t.plan:
                _LOGGER.debug(
                    "[TRANSLATE] handled plan domain=%s service=%s entity=%s score=%.3f text=%s",
                    t.plan.domain,
                    t.plan.service,
                    t.plan.entity_id,
                    t.plan.match_score,
                    t.plan.normalized_text,
                )
                if t.plan.domain == "homeassistant":
                    await self.hass.services.async_call(
                        "homeassistant",
                        t.plan.service,
                        {"entity_id": t.plan.entity_id},
                        blocking=True,
                    )
                elif t.plan.domain == "climate" and t.plan.service == "set_temperature":
                    await self.hass.services.async_call(
                        "climate",
                        "set_temperature",
                        {"entity_id": t.plan.entity_id, "temperature": t.plan.value},
                        blocking=True,
                    )
                else:
                    await self.hass.services.async_call(
                        t.plan.domain,
                        t.plan.service,
                        {"entity_id": t.plan.entity_id},
                        blocking=True,
                    )

                # Return a local "action_done" style response
                intent_response = intent.IntentResponse(language=user_input.language)
                intent_response.async_set_speech("Done")
                return conversation.ConversationResult(
                    conversation_id=user_input.conversation_id,
                    response=intent_response,
                )
        except Exception as err:
            _LOGGER.warning("Translation layer error (falling back to agents): %s", err, exc_info=True)

        # 2) Otherwise, route through configured agent chain
        agents = [
            self.entry.options.get(CONF_PRIMARY_AGENT),
            self.entry.options.get(CONF_FALLBACK_AGENT),
        ]

        # Debug: show what the system thinks exists
        if debug_level != DEBUG_LEVEL_NO_DEBUG:
            _LOGGER.debug(
                "[DEBUG] Configured agent refs=%s resolved_ids=%s available_ids=%s",
                agents,
                [self._resolve_agent_ref(a, id_map) for a in agents],
                sorted({v for v in id_map.values()}),
            )

        all_results: list[conversation.ConversationResult] = []
        result: conversation.ConversationResult | None = None

        for agent_ref in agents:
            resolved_id = self._resolve_agent_ref(agent_ref, id_map)
            if resolved_id is None:
                _LOGGER.warning("[DEBUG] Could not resolve configured agent ref '%s'. Skipping.", agent_ref)
                continue

            agent_name = name_map.get(agent_ref, name_map.get(resolved_id, "[unknown]"))

            result = await self._async_process_agent(
                agent_manager,
                resolved_id,
                agent_name,
                user_input,
                debug_level,
                result,
                agent_ref=agent_ref,
                available_ids=sorted({v for v in id_map.values()}),
            )

            plain = result.response.speech.get("plain") or {}
            orig = (plain.get("original_speech") or plain.get("speech") or "").lower()

            if (
                result.response.response_type != intent.IntentResponseType.ERROR
                and orig not in STRANGE_ERROR_RESPONSES
            ):
                return result

            all_results.append(result)

        # 3) Complete failure (everyone errored or could not be resolved)
        intent_response = intent.IntentResponse(language=user_input.language)
        err_msg = "Complete fallback failure. No Conversation Agent was able to respond."

        if all_results and debug_level in (DEBUG_LEVEL_LOW_DEBUG, DEBUG_LEVEL_VERBOSE_DEBUG):
            if debug_level == DEBUG_LEVEL_LOW_DEBUG:
                r = all_results[-1].response.speech["plain"]
                err_msg += f"\n{r.get('agent_name', 'UNKNOWN')} responded with: {r.get('original_speech', r.get('speech',''))}"
            else:
                for res in all_results:
                    r = res.response.speech["plain"]
                    err_msg += f"\n{r.get('agent_name', 'UNKNOWN')} responded with: {r.get('original_speech', r.get('speech',''))}"

        intent_response.async_set_error(
            intent.IntentResponseErrorCode.NO_INTENT_MATCH,
            err_msg,
        )

        return conversation.ConversationResult(
            conversation_id=user_input.conversation_id,
            response=intent_response,
        )

    async def _async_process_agent(
        self,
        agent_manager: conversation.AgentManager,
        agent_id: str,
        agent_name: str,
        user_input: conversation.ConversationInput,
        debug_level: int,
        previous_result: conversation.ConversationResult | None,
        *,
        agent_ref: str | None = None,
        available_ids: list[str] | None = None,
    ) -> conversation.ConversationResult:
        """Process a specified agent."""
        # Resolve entity_id-like selector values to AgentManager ids
        original_agent_id = agent_id

        # DEBUG: show what we're about to request
        try:
            infos = agent_manager.async_get_agent_info()
            _LOGGER.error(
                "[DEBUG] Attempting async_get_agent(%r) resolved from %r. Available ids=%s",
                agent_id,
                original_agent_id,
                [i.id for i in infos],
            )
        except Exception:
            _LOGGER.exception("[DEBUG] Failed to inspect AgentManager before lookup")

        # Built-in Home Assistant agent is not always registered in AgentManager.
        if agent_id == "__homeassistant__":
            ha_agent = self._get_default_home_assistant_agent()
            if ha_agent is None:
                intent_response = intent.IntentResponse(language=user_input.language)
                intent_response.async_set_error(
                    intent.IntentResponseErrorCode.NO_INTENT_MATCH,
                    "Home Assistant agent not available",
                )
                return conversation.ConversationResult(
                    conversation_id=user_input.conversation_id,
                    response=intent_response,
                )

            result = await ha_agent.async_process(user_input)
            r = result.response.speech["plain"]["speech"]
            result.response.speech["plain"]["original_speech"] = r
            result.response.speech["plain"]["agent_name"] = agent_name or "Home Assistant"
            result.response.speech["plain"]["agent_id"] = "homeassistant"
            return result

        # SAFETY: do not crash if agent does not exist
        try:
            agent = agent_manager.async_get_agent(agent_id)
        except ValueError:
            _LOGGER.error(
                "[DEBUG] Agent '%s' not found. Skipping this agent.",
                agent_id,
            )

            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.NO_INTENT_MATCH,
                f"Agent {agent_id} not found",
            )

            return conversation.ConversationResult(
                conversation_id=user_input.conversation_id,
                response=intent_response,
            )

        _LOGGER.debug(
            "Processing in %s using %s with debug level %s: %s",
            user_input.language,
            agent_id,
            debug_level,
            user_input.text,
        )

        result = await agent.async_process(user_input)

        # Ensure speech[plain] metadata exists
        if "plain" not in result.response.speech:
            result.response.speech["plain"] = {"speech": ""}

        r = result.response.speech["plain"].get("speech", "")
        result.response.speech["plain"]["original_speech"] = r
        result.response.speech["plain"]["agent_name"] = agent_name
        result.response.speech["plain"]["agent_id"] = agent_id

        if debug_level == DEBUG_LEVEL_LOW_DEBUG:
            result.response.speech["plain"]["speech"] = f"{agent_name} responded with: {r}"
        elif debug_level == DEBUG_LEVEL_VERBOSE_DEBUG:
            if previous_result is not None:
                pr_plain = previous_result.response.speech.get("plain", {})
                pr = pr_plain.get("original_speech", pr_plain.get("speech", ""))
                prev_name = pr_plain.get("agent_name", "UNKNOWN")
                result.response.speech["plain"][
                    "speech"
                ] = f"{prev_name} failed with response: {pr} Then {agent_name} responded with {r}"
            else:
                result.response.speech["plain"]["speech"] = f"{agent_name} responded with: {r}"

        # Save result to entity, if present
        try:
            domain_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {})
            result_entity = domain_data.get("result_entity")
            if result_entity:
                result_entity.update_result(agent_name, user_input.text, result)
            else:
                _LOGGER.debug(
                    "Result entity not found. Sensor platform may not be initialized yet."
                )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to update result entity", exc_info=True)

        return result
