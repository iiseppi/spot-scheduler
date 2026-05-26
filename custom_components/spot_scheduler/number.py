"""Config number entities for SpotScheduler (appear in Configuration section on device page)."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_AUTO_SELECT_END_HOUR,
    CONF_AUTO_SELECT_HOURS,
    CONF_AUTO_SELECT_START_HOUR,
    CONF_EXPENSIVE_HOURS_COUNT,
    CONF_PRICE_THRESHOLD_HIGH,
    CONF_PRICE_THRESHOLD_LOW,
    DEFAULT_AUTO_SELECT_END_HOUR,
    DEFAULT_AUTO_SELECT_HOURS,
    DEFAULT_AUTO_SELECT_START_HOUR,
    DEFAULT_EXPENSIVE_HOURS,
    DEFAULT_PRICE_THRESHOLD_HIGH,
    DEFAULT_PRICE_THRESHOLD_LOW,
    DOMAIN,
)

if __name__ != "__main__":
    from . import SpotSchedulerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpotSchedulerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SpotScheduler config number entities."""
    entities = [
        SpotPriceThresholdLowNumber(entry),
        SpotPriceThresholdHighNumber(entry),
        SpotAutoSelectHoursNumber(entry),
        SpotAutoSelectStartHourNumber(entry),
        SpotAutoSelectEndHourNumber(entry),
        SpotExpensiveHoursNumber(entry),
    ]
    async_add_entities(entities)

    # Remove stale number entities (e.g. old per-device auto-select numbers)
    ent_reg = er.async_get(hass)
    current_unique_ids = {e.unique_id for e in entities}
    stale = [
        e
        for e in ent_reg.entities.values()
        if e.config_entry_id == entry.entry_id
        and e.platform == DOMAIN
        and e.domain == "number"
        and e.unique_id not in current_unique_ids
    ]
    for stale_entity in stale:
        ent_reg.async_remove(stale_entity.entity_id)


class _SpotConfigNumber(NumberEntity):
    """Base class for SpotScheduler config number entities."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_has_entity_name = True

    def __init__(self, entry: SpotSchedulerConfigEntry) -> None:
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get("name", "SpotScheduler"),
            manufacturer="SpotScheduler",
            model="Spot Price Scheduler",
        )

    def _merged(self) -> dict:
        return {**self._entry.data, **self._entry.options}

    async def _save_option(self, key: str, value: object) -> None:
        new_opts = {**self._entry.options, key: value}
        self.hass.config_entries.async_update_entry(self._entry, options=new_opts)
        self.async_write_ha_state()


class SpotPriceThresholdLowNumber(_SpotConfigNumber):
    """Price below which hours are shown green."""

    _attr_translation_key = "price_threshold_low"
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "c/kWh"
    _attr_icon = "mdi:hand-coin-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_price_threshold_low"

    @property
    def native_value(self) -> float:
        return float(self._merged().get(CONF_PRICE_THRESHOLD_LOW, DEFAULT_PRICE_THRESHOLD_LOW))

    async def async_set_native_value(self, value: float) -> None:
        await self._save_option(CONF_PRICE_THRESHOLD_LOW, float(value))


class SpotPriceThresholdHighNumber(_SpotConfigNumber):
    """Price above which hours are shown red."""

    _attr_translation_key = "price_threshold_high"
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "c/kWh"
    _attr_icon = "mdi:fire"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_price_threshold_high"

    @property
    def native_value(self) -> float:
        return float(self._merged().get(CONF_PRICE_THRESHOLD_HIGH, DEFAULT_PRICE_THRESHOLD_HIGH))

    async def async_set_native_value(self, value: float) -> None:
        await self._save_option(CONF_PRICE_THRESHOLD_HIGH, float(value))


class SpotAutoSelectHoursNumber(_SpotConfigNumber):
    """Global count of cheapest hours to auto-select for all devices."""

    _attr_translation_key = "auto_select_hours"
    _attr_native_min_value = 0
    _attr_native_max_value = 12
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "h"
    _attr_icon = "mdi:clock-check-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_auto_select_hours_global"

    @property
    def native_value(self) -> float:
        raw = self._merged().get(CONF_AUTO_SELECT_HOURS, DEFAULT_AUTO_SELECT_HOURS)
        # Handle legacy per-device dict: fall back to 0 (disabled)
        if isinstance(raw, dict):
            return 0.0
        return float(int(raw)) if isinstance(raw, (int, float)) else 0.0

    async def async_set_native_value(self, value: float) -> None:
        await self._save_option(CONF_AUTO_SELECT_HOURS, int(value))


class SpotAutoSelectStartHourNumber(_SpotConfigNumber):
    """Start hour for cheapest-hour auto-select window."""

    _attr_translation_key = "auto_select_start_hour"
    _attr_native_min_value = 0
    _attr_native_max_value = 23
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "h"
    _attr_icon = "mdi:clock-start"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_auto_select_start_hour"

    @property
    def native_value(self) -> float:
        return float(
            int(
                self._merged().get(
                    CONF_AUTO_SELECT_START_HOUR,
                    DEFAULT_AUTO_SELECT_START_HOUR,
                )
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._save_option(CONF_AUTO_SELECT_START_HOUR, int(value))


class SpotAutoSelectEndHourNumber(_SpotConfigNumber):
    """End hour for cheapest-hour auto-select window.

    End hour is exclusive:
    start=0, end=7 means 00:00–06:59.
    """

    _attr_translation_key = "auto_select_end_hour"
    _attr_native_min_value = 1
    _attr_native_max_value = 24
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "h"
    _attr_icon = "mdi:clock-end"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_auto_select_end_hour"

    @property
    def native_value(self) -> float:
        return float(
            int(
                self._merged().get(
                    CONF_AUTO_SELECT_END_HOUR,
                    DEFAULT_AUTO_SELECT_END_HOUR,
                )
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._save_option(CONF_AUTO_SELECT_END_HOUR, int(value))


class SpotExpensiveHoursNumber(_SpotConfigNumber):
    """Number of expensive hours to highlight in red."""

    _attr_translation_key = "expensive_hours"
    _attr_native_min_value = 0
    _attr_native_max_value = 12
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "h"
    _attr_icon = "mdi:weather-sunset-up"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_expensive_hours"

    @property
    def native_value(self) -> float:
        return float(self._merged().get(CONF_EXPENSIVE_HOURS_COUNT, DEFAULT_EXPENSIVE_HOURS))

    async def async_set_native_value(self, value: float) -> None:
        await self._save_option(CONF_EXPENSIVE_HOURS_COUNT, int(value))
