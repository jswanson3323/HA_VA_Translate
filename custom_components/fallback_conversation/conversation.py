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

        # Always initialize; avoids UnboundLocalError when no agents resolve.
        result: conversation.ConversationResult | None = None
        # Friendly names for logging / debug
        agent_names: dict[str, str] = {}
        for info in infos:
            agent_names[info.id] = info.name or "[unknown]"

        # Build a resolver that maps:
        # - AgentManager internal ids (ULIDs)
        # - conversation entity_ids (e.g. conversation.home_assistant)
        # - short entity ids (e.g. home_assistant)
        # - human names (e.g. "Home Assistant")
        # -> internal id used by AgentManager
        infos = agent_manager.async_get_agent_info()
        id_map: dict[str, str] = {}
        name_map: dict[str, str] = {}

        for info in infos:
            internal_id = info.id
            id_map[internal_id] = internal_id

            try:
                agent_obj = agent_manager.async_get_agent(internal_id)
                reg_entry = getattr(agent_obj, "registry_entry", None)
                ent_id = getattr(reg_entry, "entity_id", None) if reg_entry else None
                if ent_id:
                    id_map[ent_id] = internal_id
                    # allow storing without "conversation."
                    if ent_id.startswith("conversation."):
                        id_map[ent_id.split(".", 1)[1]] = internal_id
            except Exception:
                # Don't block on weird agents; we'll just skip extra mappings.
                pass

            if info.name:
                name_map[info.name.lower()] = internal_id

        def _resolve_agent_id(raw: str | None) -> str | None:
            if not raw:
                return None
            raw_s = str(raw).strip()
            if not raw_s:
                return None

            # direct mappings
            if raw_s in id_map:
                return id_map[raw_s]

            # case-insensitive name match
            lower = raw_s.lower()
            if lower in name_map:
                return name_map[lower]

            return None

        # Read configured agent refs (options override data)
        primary_raw = self.entry.options.get(CONF_PRIMARY_AGENT) or self.entry.data.get(CONF_PRIMARY_AGENT)
        fallback_raw = self.entry.options.get(CONF_FALLBACK_AGENT) or self.entry.data.get(CONF_FALLBACK_AGENT)

        primary_id = _resolve_agent_id(primary_raw)
        fallback_id = _resolve_agent_id(fallback_raw)

        _LOGGER.error(
            "[ROUTER] primary_raw=%s primary_id=%s fallback_raw=%s fallback_id=%s available_internal_ids=%s",
            primary_raw,
            primary_id,
            fallback_raw,
            fallback_id,
            [i.id for i in infos],
        )

        # Only run agents that we can resolve
        agents: list[str] = [a for a in (primary_id, fallback_id) if a]

        debug_level = (
            self.entry.options.get(CONF_DEBUG_LEVEL)
            if self.entry.options.get(CONF_DEBUG_LEVEL) is not None
            else self.entry.data.get(CONF_DEBUG_LEVEL, DEBUG_LEVEL_NO_DEBUG)
        )

        if user_input.conversation_id is None:
            user_input.conversation_id = ulid.ulid()

        # --- Translation layer (deterministic) ---
        try:
            catalog = await async_get_exposed_catalog(self.hass, assistant="conversation")
            items = await catalog.async_get_items()
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {})
            dialog_phrases = entry_data.get("dialog_phrases", [])
            enable_dialog_bypass = self.entry.options.get(
                CONF_ENABLE_DIALOG_BYPASS,
                self.entry.data.get(CONF_ENABLE_DIALOG_BYPASS, DEFAULT_ENABLE_DIALOG_BYPASS),
            )
            dialog_bypass_min_score = float(
                self.entry.options.get(
                    CONF_DIALOG_BYPASS_MIN_SCORE,
                    self.entry.data.get(
                        CONF_DIALOG_BYPASS_MIN_SCORE,
                        DEFAULT_DIALOG_BYPASS_MIN_SCORE,
                    ),
                )
            )

            t_res = translate_to_action(
                user_input.text,
                items,
                dialog_phrases=dialog_phrases,
                enable_dialog_bypass=bool(enable_dialog_bypass),
                dialog_bypass_min_score=dialog_bypass_min_score,
            )

            if t_res.handled and t_res.plan:
                plan = t_res.plan

                service_data: dict[str, object] = {"entity_id": plan.entity_id}
                # temperature setter for climate
                if (
                    plan.domain == "climate"
                    and plan.service == "set_temperature"
                    and plan.value is not None
                ):
                    service_data["temperature"] = plan.value

                await self.hass.services.async_call(
                    plan.domain,
                    plan.service,
                    service_data,
                    blocking=True,
                )

                intent_response = intent.IntentResponse(language=user_input.language)
                intent_response.async_set_speech(_action_speech_from_plan(plan))

                return conversation.ConversationResult(
                    conversation_id=user_input.conversation_id,
                    response=intent_response,
                )

        except Exception as ex:  # noqa: BLE001
            _LOGGER.exception("Translation layer error (falling back to agents): %s", ex)

        all_results: list[conversation.ConversationResult] = []
        result: conversation.ConversationResult | None = None

        for agent_id in agents:
            agent_name = agent_names.get(agent_id, "[unknown]")
            if agent_name == "[unknown]":
                _LOGGER.warning("agent_name not found for agent_id %s", agent_id)

            result = await self._async_process_agent(
                agent_manager,
                agent_id,
                agent_name,
                user_input,
                debug_level,
                result,
            )

            plain = result.response.speech.get("plain", {}) if result.response else {}
            original = plain.get("original_speech", "")

            if (
                result.response.response_type != intent.IntentResponseType.ERROR
                and str(original).lower() not in STRANGE_ERROR_RESPONSES
            ):
                return result

            all_results.append(result)

        # Complete failure
        intent_response = intent.IntentResponse(language=user_input.language)
        err = "Complete fallback failure. No Conversation Agent was able to respond."

        if all_results:
            if debug_level == DEBUG_LEVEL_LOW_DEBUG:
                r = all_results[-1].response.speech["plain"]
                err += (
                    f"\n{r.get('agent_name', 'UNKNOWN')} responded with: "
                    f"{r.get('original_speech', r.get('speech', ''))}"
                )
            elif debug_level == DEBUG_LEVEL_VERBOSE_DEBUG:
                for res in all_results:
                    r = res.response.speech["plain"]
                    err += (
                        f"\n{r.get('agent_name', 'UNKNOWN')} responded with: "
                        f"{r.get('original_speech', r.get('speech', ''))}"
                    )

        intent_response.async_set_error(
            intent.IntentResponseErrorCode.NO_INTENT_MATCH,
            err,
        )

        return conversation.ConversationResult(
            conversation_id=(result.conversation_id if result else user_input.conversation_id),
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
