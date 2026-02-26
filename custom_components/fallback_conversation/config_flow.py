"""Config flow for Fallback Conversation integration."""
from __future__ import annotations

import logging
# from types import MappingProxyType
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.components import conversation
from homeassistant.helpers.selector import (
    ConversationAgentSelector,
    ConversationAgentSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
    SelectSelectorMode,
)

from .const import (
    CONF_DEBUG_LEVEL,
    CONF_PRIMARY_AGENT,
    CONF_FALLBACK_AGENT,
    DEBUG_LEVEL_NO_DEBUG,
    DEBUG_LEVEL_LOW_DEBUG,
    DEBUG_LEVEL_VERBOSE_DEBUG,
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_DEBUG_LEVEL,
)

_LOGGER = logging.getLogger(__name__)

def _user_step_schema(
    *,
    name_default: str,
    debug_default: int,
    primary_default: str,
    fallback_default: str,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=name_default): str,
            vol.Optional(CONF_DEBUG_LEVEL, default=debug_default): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=DEBUG_LEVEL_NO_DEBUG, label="No Debug"),
                        SelectOptionDict(value=DEBUG_LEVEL_LOW_DEBUG, label="Some Debug"),
                        SelectOptionDict(value=DEBUG_LEVEL_VERBOSE_DEBUG, label="Verbose Debug"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
            vol.Optional(CONF_PRIMARY_AGENT, default=primary_default): ConversationAgentSelector(
                ConversationAgentSelectorConfig()
            ),
            vol.Optional(CONF_FALLBACK_AGENT, default=fallback_default): ConversationAgentSelector(
                ConversationAgentSelectorConfig()
            ),
        }
    )

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Fallback Agent config flow."""

    VERSION = 2

    def _resolve_selected_agent_id(self, value: Any) -> str:
        """Resolve selector value (agent id or conversation entity_id) to an AgentManager agent id."""
        agent_manager = conversation.get_agent_manager(self.hass)

        raw = str(value).strip()
        raw_lc = raw.lower()

        # Selector often returns an entity_id for the built-in HA agent
        if raw_lc in ("conversation.home_assistant", "conversation.homeassistant"):
            return "homeassistant"

        # If it's a conversation entity_id, strip the prefix and try again
        if raw_lc.startswith("conversation."):
            raw = raw.split(".", 1)[1].strip()

        # If already a valid AgentManager id, keep it
        try:
            agent_manager.async_get_agent(raw)
            return raw
        except ValueError:
            pass

        # Common built-in id fallback
        if raw.lower() in ("homeassistant", "home_assistant"):
            return "homeassistant"

        return raw

    def _default_llm_agent_id(self) -> str:
        """Pick a default non-Home Assistant conversation agent if available."""
        agent_manager = conversation.get_agent_manager(self.hass)
        for info in agent_manager.async_get_agent_info():
            if info.id != conversation.const.HOME_ASSISTANT_AGENT:
                return info.id
        return conversation.const.HOME_ASSISTANT_AGENT

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        _LOGGER.debug("ConfigFlow::user_input %s", user_input)
        if user_input is None:
            ha_agent_id = conversation.const.HOME_ASSISTANT_AGENT
            llm_default = self._default_llm_agent_id()
            return self.async_show_form(
                step_id="user",
                data_schema=_user_step_schema(
                    name_default=DEFAULT_NAME,
                    debug_default=DEFAULT_DEBUG_LEVEL,
                    primary_default=ha_agent_id,
                    fallback_default=llm_default,
                ),
            )

        # Resolve and store usable AgentManager ids
        if CONF_PRIMARY_AGENT in user_input:
            user_input[CONF_PRIMARY_AGENT] = self._resolve_selected_agent_id(
                user_input[CONF_PRIMARY_AGENT]
            )
        if CONF_FALLBACK_AGENT in user_input:
            user_input[CONF_FALLBACK_AGENT] = self._resolve_selected_agent_id(
                user_input[CONF_FALLBACK_AGENT]
            )

        return self.async_create_entry(
            title=user_input.get(CONF_NAME, DEFAULT_NAME),
            data=user_input,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlow(config_entry)

class OptionsFlow(config_entries.OptionsFlow):
    """Fallback config flow options handler."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self._options = dict(config_entry.data)
        self._options.update(dict(config_entry.options))

    def _resolve_selected_agent_id(self, value: Any) -> str:
        """Resolve selector value (agent id or conversation entity_id) to an AgentManager agent id."""
        agent_manager = conversation.get_agent_manager(self.hass)

        raw = str(value).strip()
        raw_lc = raw.lower()

        if raw_lc in ("conversation.home_assistant", "conversation.homeassistant"):
            return "homeassistant"

        if raw_lc.startswith("conversation."):
            raw = raw.split(".", 1)[1].strip()

        try:
            agent_manager.async_get_agent(raw)
            return raw
        except ValueError:
            pass

        if raw.lower() in ("homeassistant", "home_assistant"):
            return "homeassistant"

        return raw

    def _default_llm_agent_id(self) -> str:
        """Pick a default non-Home Assistant conversation agent if available."""
        agent_manager = conversation.get_agent_manager(self.hass)
        for info in agent_manager.async_get_agent_info():
            if info.id != conversation.const.HOME_ASSISTANT_AGENT:
                return info.id
        return conversation.const.HOME_ASSISTANT_AGENT

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            # Resolve and store usable AgentManager ids
            if CONF_PRIMARY_AGENT in user_input:
                user_input[CONF_PRIMARY_AGENT] = self._resolve_selected_agent_id(
                    user_input[CONF_PRIMARY_AGENT]
                )
            if CONF_FALLBACK_AGENT in user_input:
                user_input[CONF_FALLBACK_AGENT] = self._resolve_selected_agent_id(
                    user_input[CONF_FALLBACK_AGENT]
                )

            self._options.update(user_input)
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data=self._options,
            )

        schema = await self.fallback_config_option_schema(self._options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
        )

    async def fallback_config_option_schema(self, options: dict) -> dict:
        """Return a schema for Fallback options."""
        ha_agent_id = conversation.const.HOME_ASSISTANT_AGENT
        llm_default = self._default_llm_agent_id()

        return {
            vol.Required(
                CONF_DEBUG_LEVEL,
                description={"suggested_value": options.get(CONF_DEBUG_LEVEL, DEFAULT_DEBUG_LEVEL)},
                default=DEFAULT_DEBUG_LEVEL,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=DEBUG_LEVEL_NO_DEBUG, label="No Debug"),
                        SelectOptionDict(value=DEBUG_LEVEL_LOW_DEBUG, label="Some Debug"),
                        SelectOptionDict(value=DEBUG_LEVEL_VERBOSE_DEBUG, label="Verbose Debug"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                ),
            ),
            vol.Required(
                CONF_PRIMARY_AGENT,
                description={"suggested_value": options.get(CONF_PRIMARY_AGENT, ha_agent_id)},
                default=ha_agent_id,
            ): ConversationAgentSelector(ConversationAgentSelectorConfig()),
            vol.Required(
                CONF_FALLBACK_AGENT,
                description={"suggested_value": options.get(CONF_FALLBACK_AGENT, llm_default)},
                default=llm_default,
            ): ConversationAgentSelector(ConversationAgentSelectorConfig()),
        }