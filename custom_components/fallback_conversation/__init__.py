"""Fallback Conversation Agent"""
from __future__ import annotations

import asyncio

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_REBUILD_DIALOG_CATALOG
from .dialog_catalog import async_rebuild_dialog_phrases

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.CONVERSATION, Platform.SENSOR]

# hass.data key for agent.
DATA_AGENT = "agent"
_DATA_UNSUBS = "unsubs"
_DATA_SERVICE_REGISTERED = "_service_registered"
_EVENT_AUTOMATION_RELOADED = "automation_reloaded"


def _entry_options(entry: ConfigEntry) -> dict:
    options = dict(entry.data)
    options.update(dict(entry.options))
    return options


async def _async_rebuild_entry_dialog_catalog(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await async_rebuild_dialog_phrases(hass, entry.entry_id, _entry_options(entry))


async def _async_handle_rebuild_service(hass: HomeAssistant, _call: ServiceCall) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        await _async_rebuild_entry_dialog_catalog(hass, entry)
        stats = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("dialog_phrase_stats", {})
        _LOGGER.info(
            "Rebuilt dialog catalog for entry %s: total=%s yaml=%s conversation_triggers=%s",
            entry.entry_id,
            stats.get("total", 0),
            stats.get("yaml", 0),
            stats.get("conversation_triggers", 0),
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Fallback Conversation from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {_DATA_UNSUBS: []}

    entry_data = hass.data[DOMAIN][entry.entry_id]

    if hass.is_running:
        await _async_rebuild_entry_dialog_catalog(hass, entry)
    else:

        async def _async_started_reload(event: Event) -> None:
            _LOGGER.debug(
                "Home Assistant started; rebuilding dialog catalog for %s", entry.entry_id
            )
            await _async_rebuild_entry_dialog_catalog(hass, entry)

        @callback
        def _handle_started(event: Event) -> None:
            hass.async_create_task(_async_started_reload(event))

        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            _handle_started,
        )
        entry_data[_DATA_UNSUBS].append(unsub_started)

    def _automation_reloaded(_event: Event) -> None:
        _LOGGER.debug(
            "automation_reloaded received; rebuilding dialog catalog for %s",
            entry.entry_id,
        )
        hass.async_create_task(_async_rebuild_entry_dialog_catalog(hass, entry))

    unsub_automation = hass.bus.async_listen(_EVENT_AUTOMATION_RELOADED, _automation_reloaded)
    entry_data[_DATA_UNSUBS].append(unsub_automation)

    if not hass.data[DOMAIN].get(_DATA_SERVICE_REGISTERED):
        async def _handle_rebuild_service(call: ServiceCall) -> None:
            await _async_handle_rebuild_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_REBUILD_DIALOG_CATALOG,
            _handle_rebuild_service,
        )
        hass.data[DOMAIN][_DATA_SERVICE_REGISTERED] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    for unsub in hass.data[DOMAIN].get(entry.entry_id, {}).get(_DATA_UNSUBS, []):
        try:
            unsub()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed unsubscribing callback for %s", entry.entry_id)

    hass.data[DOMAIN].pop(entry.entry_id)

    if (
        hass.data.get(DOMAIN, {}).get(_DATA_SERVICE_REGISTERED)
        and not hass.config_entries.async_entries(DOMAIN)
        and hass.services.has_service(DOMAIN, SERVICE_REBUILD_DIALOG_CATALOG)
    ):
        hass.services.async_remove(DOMAIN, SERVICE_REBUILD_DIALOG_CATALOG)
        hass.data[DOMAIN][_DATA_SERVICE_REGISTERED] = False

    return True


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        _LOGGER.error("Cannot upgrade models that were created prior to v0.3. Please delete and re-create them.")
        return False

    _LOGGER.debug("Migration to version %s successful", config_entry.version)

    return True
