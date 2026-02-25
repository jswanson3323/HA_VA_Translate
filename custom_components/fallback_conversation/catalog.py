# custom_components/fallback_conversation/catalog.py
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import exposed_entities

_LOGGER = logging.getLogger(__name__)

DEFAULT_DOMAINS: Set[str] = {
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

CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class EntityCatalogItem:
    entity_id: str
    domain: str
    name: str                 # friendly name
    area_name: Optional[str]  # resolved area name
    device_name: Optional[str]


class ExposedEntityCatalog:
    """Catalog of entities exposed to the conversation assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        assistant: str = "conversation",
        domains: Optional[Set[str]] = None,
    ) -> None:
        self.hass = hass
        self.assistant = assistant
        self.domains = domains or set(DEFAULT_DOMAINS)

        self._lock = asyncio.Lock()
        self._built_at: float = 0.0
        self._items: List[EntityCatalogItem] = []
        self._by_id: Dict[str, EntityCatalogItem] = {}
        self._unsubs: List[callable] = []

    async def async_start(self) -> None:
        """Start listeners and build initial catalog."""
        # Exposure changes (the key one)
        self._unsubs.append(
            exposed_entities.async_listen_entity_updates(
                self.hass, self.assistant, self._on_exposed_entities_changed
            )
        )

        # Registry changes can affect names/area mapping
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)

        self._unsubs.append(ent_reg.async_listen(self._on_registry_changed))
        self._unsubs.append(dev_reg.async_listen(self._on_registry_changed))
        self._unsubs.append(area_reg.async_listen(self._on_registry_changed))

        await self.async_rebuild(force=True)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()

    async def async_get_items(self) -> List[EntityCatalogItem]:
        """Get current items, rebuilding if TTL expired."""
        if (time.time() - self._built_at) > CACHE_TTL_SECONDS:
            await self.async_rebuild()
        return self._items

    async def async_rebuild(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and (time.time() - self._built_at) <= CACHE_TTL_SECONDS:
                return

            items, by_id = await self._build()
            self._items = items
            self._by_id = by_id
            self._built_at = time.time()

            _LOGGER.debug(
                "ExposedEntityCatalog rebuilt: %d entities (assistant=%s)",
                len(items),
                self.assistant,
            )

    async def _build(self) -> tuple[List[EntityCatalogItem], Dict[str, EntityCatalogItem]]:
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)

        out: List[EntityCatalogItem] = []
        by_id: Dict[str, EntityCatalogItem] = {}

        for entry in ent_reg.entities.values():
            entity_id = entry.entity_id
            domain = entity_id.split(".", 1)[0]

            if domain not in self.domains:
                continue

            if not exposed_entities.async_should_expose(self.hass, self.assistant, entity_id):
                continue

            # Friendly name preference: registry name -> state attr -> entity_id
            friendly = entry.name
            if not friendly:
                st = self.hass.states.get(entity_id)
                friendly = st.attributes.get("friendly_name") if st else None
            if not friendly:
                friendly = entity_id

            device_name: Optional[str] = None
            area_name: Optional[str] = None

            # Resolve device + area
            area_id = entry.area_id
            if entry.device_id:
                dev = dev_reg.devices.get(entry.device_id)
                if dev:
                    device_name = dev.name_by_user or dev.name
                    area_id = area_id or dev.area_id

            if area_id:
                area = area_reg.areas.get(area_id)
                area_name = area.name if area else None

            item = EntityCatalogItem(
                entity_id=entity_id,
                domain=domain,
                name=str(friendly),
                area_name=area_name,
                device_name=device_name,
            )
            out.append(item)
            by_id[entity_id] = item

        return out, by_id

    @callback
    def _on_exposed_entities_changed(self, _entity_id: str) -> None:
        self.hass.async_create_task(self.async_rebuild(force=True))

    @callback
    def _on_registry_changed(self, _event) -> None:
        self.hass.async_create_task(self.async_rebuild(force=True))


# ---- Small convenience: store one catalog instance in hass.data ----
_DATA_KEY = "fallback_conversation_exposed_catalog"


async def async_get_exposed_catalog(
    hass: HomeAssistant,
    *,
    assistant: str = "conversation",
    domains: Optional[Set[str]] = None,
) -> ExposedEntityCatalog:
    cat: ExposedEntityCatalog | None = hass.data.get(_DATA_KEY)
    if cat is None:
        cat = ExposedEntityCatalog(hass, assistant=assistant, domains=domains)
        hass.data[_DATA_KEY] = cat
        await cat.async_start()
    return cat