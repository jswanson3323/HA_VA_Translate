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
    CONF_DEBUG_LEVEL,
    CONF_FALLBACK_AGENT,
    CONF_PRIMARY_AGENT,
    DEBUG_LEVEL_LOW_DEBUG,
    DEBUG_LEVEL_NO_DEBUG,
    DEBUG_LEVEL_VERBOSE_DEBUG,
    DOMAIN,
    STRANGE_ERROR_RESPONSES,
)
from .translator import translate_to_action

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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

    def _resolve_agent_id(
        self, agent_manager: conversation.AgentManager, agent_id: str
    ) -> str:
        """Resolve stored selector value (agent id or entity_id) to a real agent id.

        ConversationAgentSelector may store either:
        - an AgentManager id (e.g. "homeassistant") OR
        - a conversation entity_id (e.g. "conversation.home_assistant").

        AgentManager only accepts the agent id.
        """
        # Normalize (selectors/storage may add whitespace or different casing)
        agent_id_str = str(agent_id).strip()
        agent_id_lc = agent_id_str.lower()

        # Hard-map the built-in HA agent entity_id to the built-in agent id
        if agent_id_lc in ("conversation.home_assistant", "conversation.homeassistant"):
            return conversation.const.HOME_ASSISTANT_AGENT
        if agent_id_lc.startswith("conversation."):
            tail = agent_id_lc.split(".", 1)[1]
            if tail in ("home_assistant", "homeassistant"):
                return conversation.const.HOME_ASSISTANT_AGENT

        # Use stripped value from here on
        agent_id = agent_id_str

        # Try as-is first
        try:
            agent_manager.async_get_agent(agent_id)
            return agent_id
        except ValueError:
            pass

        # Map entity_id -> agent id by scanning known agents
        for info in agent_manager.async_get_agent_info():
            try:
                agent = agent_manager.async_get_agent(info.id)
            except Exception:  # noqa: BLE001
                continue

            if hasattr(agent, "registry_entry") and agent.registry_entry.entity_id == agent_id:
                return info.id

        # Last resort: strip conversation. prefix and retry
        if agent_id.startswith("conversation."):
            maybe = agent_id.split(".", 1)[1]
            try:
                agent_manager.async_get_agent(maybe)
                return maybe
            except ValueError:
                pass

        return agent_id

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
        assist_pipeline.async_migrate_engine(
            self.hass, "conversation", self.entry.entry_id, self.entity_id
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
        agent_names = self._convert_agent_info_to_dict(agent_manager.async_get_agent_info())

        default_agent_id = conversation.const.HOME_ASSISTANT_AGENT

        primary_agent_id = (
            self.entry.options.get(CONF_PRIMARY_AGENT)
            or self.entry.data.get(CONF_PRIMARY_AGENT)
            or default_agent_id
        )
        fallback_agent_id = (
            self.entry.options.get(CONF_FALLBACK_AGENT)
            or self.entry.data.get(CONF_FALLBACK_AGENT)
            or default_agent_id
        )

        agents = [primary_agent_id, fallback_agent_id]
        agents = [self._resolve_agent_id(agent_manager, str(a)) for a in agents]

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

            t_res = translate_to_action(user_input.text, items)

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
                intent_response.async_set_speech("Done.")

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
        agent_id = self._resolve_agent_id(agent_manager, str(agent_id).strip())
        agent = agent_manager.async_get_agent(agent_id)

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