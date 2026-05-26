"""Spot Scheduler – schedule devices by Nord Pool spot prices (HA core integration)."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback, Event
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util

from .const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    NORDPOOL_DOMAIN,
    CONF_NORDPOOL_CONFIG_ENTRY,
    CONF_DEVICES,
    CONF_AUTO_SELECT_HOURS,
    CONF_AUTO_SELECT_START_HOUR,
    CONF_AUTO_SELECT_END_HOUR,
    CONF_EXPENSIVE_HOURS_COUNT,
    CONF_AUTO_SELECT_ENABLED,
    CONF_BLOCK_EXPENSIVE_HOURS,
    DEFAULT_AUTO_SELECT_HOURS,
    DEFAULT_AUTO_SELECT_START_HOUR,
    DEFAULT_AUTO_SELECT_END_HOUR,
    DEFAULT_AUTO_SELECT_ENABLED,
    DEFAULT_EXPENSIVE_HOURS,
    DEFAULT_BLOCK_EXPENSIVE,
    ISSUE_NORDPOOL_MISSING,
    ISSUE_NORDPOOL_UNAVAILABLE,
    TOMORROW_POLL_INTERVAL_MINUTES,
    TOMORROW_POLL_START_HOUR,
)
from .logic import parse_hourly_prices, prune_old_dates, set_schedule, cheapest_hours, expensive_hours

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR, Platform.NUMBER]


@dataclass
class SpotSchedulerData:
    """Runtime data for a SpotScheduler config entry."""

    store: Store
    schedules: dict
    prices: dict
    min_price: float | None = None
    max_price: float | None = None
    tomorrow_fetched: bool = False
    tomorrow_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    configured_devices: set[str] = field(default_factory=set)
    prices_in_storage: set[str] = field(default_factory=set)
    auto_selected: set[str] = field(default_factory=set)


type SpotSchedulerConfigEntry = ConfigEntry[SpotSchedulerData]


def _get_nordpool_entry_id(entry: SpotSchedulerConfigEntry) -> str | None:
    """Get Nord Pool config entry ID, preferring options over data."""
    return (
        entry.options.get(CONF_NORDPOOL_CONFIG_ENTRY)
        or entry.data.get(CONF_NORDPOOL_CONFIG_ENTRY)
    )


def _coerce_enabled(value):
    """Accept true/false (on/off) or 'skip' (explicit don't touch). Absent = use default."""
    if value is None:
        return None
    if value == "skip":
        return "skip"
    return cv.boolean(value)


SET_SCHEDULE_SCHEMA = vol.Schema({
    vol.Optional("date"): cv.date,
    vol.Required("hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("device_id"): cv.entity_id,
    vol.Optional("enabled"): _coerce_enabled,
})

REFRESH_PRICES_SCHEMA = vol.Schema({
    vol.Optional("date"): cv.date,
})

APPLY_NOW_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): cv.string,
})

RUN_AUTO_SELECT_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): cv.string,
    vol.Optional("date"): vol.Maybe(cv.date),
})


async def async_setup_entry(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> bool:
    """Set up SpotScheduler from a config entry."""
    hass.data.setdefault(DOMAIN, set())

    from . import frontend
    await frontend.async_register(hass)

    nordpool_entry_id = _get_nordpool_entry_id(entry)
    if not hass.config_entries.async_get_entry(nordpool_entry_id):
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_NORDPOOL_MISSING,
            is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key="nordpool_integration_missing",
            translation_placeholders={"entry_id": nordpool_entry_id or "unknown"},
        )
        _LOGGER.error("Nord Pool config entry %s not found.", nordpool_entry_id)
        return False

    ir.async_delete_issue(hass, DOMAIN, ISSUE_NORDPOOL_MISSING)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_NORDPOOL_UNAVAILABLE)

    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    stored = await store.async_load() or {}

    merged_cfg = {**entry.data, **entry.options}
    loaded_prices: dict = {
        date: {int(h): p for h, p in hours.items()}
        for date, hours in stored.get("prices", {}).items()
    }

    today_str = dt_util.now().date().isoformat()
    today_price_vals = list(loaded_prices.get(today_str, {}).values())

    entry.runtime_data = SpotSchedulerData(
        store=store,
        schedules=stored.get("schedules", {}),
        prices=loaded_prices,
        min_price=min(today_price_vals) if today_price_vals else None,
        max_price=max(today_price_vals) if today_price_vals else None,
        configured_devices=set(merged_cfg.get(CONF_DEVICES, [])),
        prices_in_storage={
            d for d, hours in loaded_prices.items() if len(hours) >= 20
        },
    )
    hass.data[DOMAIN].add(entry.entry_id)

    today = dt_util.now().date()
    await _fetch_prices_for_date(hass, entry, today - timedelta(days=1), auto_select=False)

    ok = await _fetch_prices_for_date(hass, entry, today)
    if not ok:
        _maybe_raise_unavailable_issue(hass)

    if dt_util.now().hour >= TOMORROW_POLL_START_HOUR:
        tomorrow = dt_util.now().date() + timedelta(days=1)
        tok = await _fetch_prices_for_date(hass, entry, tomorrow)
        if tok:
            entry.runtime_data.tomorrow_fetched = True
            _LOGGER.info("Tomorrow's prices fetched on startup (%s)", tomorrow)

    _setup_nordpool_tracking(hass, entry, nordpool_entry_id)

    poll_minutes = list(range(0, 60, TOMORROW_POLL_INTERVAL_MINUTES))

    @callback
    def _poll_tomorrow_cb(_now) -> None:
        hass.async_create_task(_poll_tomorrow_if_needed(hass, entry))

    cancel_poll = async_track_time_change(
        hass,
        _poll_tomorrow_cb,
        hour=list(range(TOMORROW_POLL_START_HOUR, 24)),
        minute=poll_minutes,
        second=30,
    )
    entry.async_on_unload(cancel_poll)

    @callback
    def _midnight_cb(_now) -> None:
        hass.async_create_task(_on_midnight(hass, entry))

    cancel_midnight = async_track_time_change(
        hass,
        _midnight_cb,
        hour=0,
        minute=0,
        second=15,
    )
    entry.async_on_unload(cancel_midnight)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)

    _LOGGER.info("SpotScheduler started (Nord Pool entry: %s)", nordpool_entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> bool:
    """Unload SpotScheduler cleanly."""
    remaining = [eid for eid in hass.data.get(DOMAIN, set()) if eid != entry.entry_id]

    if not remaining:
        for svc in ("set_device_schedule", "refresh_prices", "apply_schedules_now", "run_auto_select"):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].discard(entry.entry_id)

    return ok


async def _async_update_listener(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> None:
    """Reload only when the device list changes."""
    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return

    data = entry.runtime_data
    configured = data.configured_devices
    current = set({**entry.data, **entry.options}.get(CONF_DEVICES, []))

    if configured != current:
        _LOGGER.info(
            "SpotScheduler: device list changed (%s → %s), reloading.",
            configured,
            current,
        )
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        hass.bus.async_fire(f"{DOMAIN}_apply_now", {"entry_id": entry.entry_id})


def _setup_nordpool_tracking(
    hass: HomeAssistant, entry: SpotSchedulerConfigEntry, nordpool_entry_id: str
) -> None:
    """Watch all Nord Pool entities for this config entry."""
    ent_reg = er.async_get(hass)
    nordpool_entities = [
        e.entity_id
        for e in ent_reg.entities.values()
        if e.config_entry_id == nordpool_entry_id
    ]

    if not nordpool_entities:
        _LOGGER.warning(
            "No Nord Pool entities found for entry %s – will rely on scheduled polling only.",
            nordpool_entry_id,
        )
        return

    @callback
    def _on_nordpool_update(event: Event) -> None:
        hass.async_create_task(_poll_tomorrow_if_needed(hass, entry))

    cancel = async_track_state_change_event(
        hass, nordpool_entities, _on_nordpool_update
    )
    entry.async_on_unload(cancel)

    _LOGGER.debug(
        "Tracking %d Nord Pool entities for entry %s",
        len(nordpool_entities),
        nordpool_entry_id,
    )


async def _poll_tomorrow_if_needed(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> None:
    """Fetch tomorrow's prices if not already fetched today."""
    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return

    data = entry.runtime_data

    if data.tomorrow_fetched:
        _LOGGER.debug("Tomorrow already fetched, skipping poll")
        return

    if dt_util.now().hour < TOMORROW_POLL_START_HOUR:
        _LOGGER.debug(
            "Too early to poll tomorrow (hour=%d, start=%d)",
            dt_util.now().hour,
            TOMORROW_POLL_START_HOUR,
        )
        return

    lock = data.tomorrow_lock
    if lock.locked():
        return

    async with lock:
        if data.tomorrow_fetched:
            return

        tomorrow = dt_util.now().date() + timedelta(days=1)
        tomorrow_prices = data.prices.get(tomorrow.isoformat(), {})

        if len(tomorrow_prices) >= 20:
            data.tomorrow_fetched = True
            return

        ok = await _fetch_prices_for_date(hass, entry, tomorrow)
        if ok:
            data.tomorrow_fetched = True
            _LOGGER.info("Tomorrow's prices fetched successfully (%s)", tomorrow)


async def _on_midnight(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> None:
    """New day: prune stale data, reset tomorrow guard, fetch today."""
    await _daily_reset(hass, entry)

    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return

    entry.runtime_data.tomorrow_fetched = False

    ok = await _fetch_prices_for_date(hass, entry, dt_util.now().date())
    if not ok and entry.entry_id in hass.data.get(DOMAIN, set()):
        _maybe_raise_unavailable_issue(hass)


async def _fetch_prices_for_date(
    hass: HomeAssistant,
    entry: SpotSchedulerConfigEntry,
    target_date: date,
    *,
    auto_select: bool = True,
) -> bool:
    """Call nordpool.get_prices_for_date and store hourly averages by local date."""
    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return False

    nordpool_entry_id = _get_nordpool_entry_id(entry)
    date_str = target_date.isoformat()

    _LOGGER.debug(
        "Fetching prices: nordpool entry=%s, date=%s",
        nordpool_entry_id,
        date_str,
    )

    try:
        result = await hass.services.async_call(
            NORDPOOL_DOMAIN,
            "get_prices_for_date",
            {"config_entry": nordpool_entry_id, "date": date_str},
            blocking=True,
            return_response=True,
        )
    except Exception as exc:
        _LOGGER.warning(
            "nordpool.get_prices_for_date failed for %s (entry=%s): %s",
            date_str,
            nordpool_entry_id,
            exc,
        )
        return False

    if not result:
        _LOGGER.warning("Empty response from nordpool for %s (entry=%s)", date_str, nordpool_entry_id)
        return False

    _LOGGER.debug(
        "Nord Pool raw response keys: %s",
        list(result.keys()) if isinstance(result, dict) else type(result),
    )

    tz = dt_util.get_time_zone(hass.config.time_zone)
    all_by_date: dict[str, dict[int, float]] = {}

    try:
        for area_data in result.values():
            if not isinstance(area_data, list):
                continue

            parsed = parse_hourly_prices(area_data, tz)
            _LOGGER.debug(
                "Nord Pool %s: %d raw slots → local dates/hours: %s",
                date_str,
                len(area_data),
                {d: sorted(h.keys()) for d, h in parsed.items()},
            )

            for local_date, hours in parsed.items():
                all_by_date.setdefault(local_date, {}).update(hours)

    except Exception as exc:
        _LOGGER.error("Failed to parse Nord Pool price data for %s: %s", date_str, exc)
        return False

    if not all_by_date:
        _LOGGER.debug("No usable price slots in Nord Pool response for %s", date_str)
        return False

    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return False

    data = entry.runtime_data

    prices_are_new = (
        auto_select
        and date_str not in data.auto_selected
        and date_str not in data.prices_in_storage
    )

    for local_date, hours in all_by_date.items():
        data.prices.setdefault(local_date, {}).update(hours)

    today_str = dt_util.now().date().isoformat()
    if date_str == today_str:
        today_prices = list(data.prices.get(today_str, {}).values())
        if today_prices:
            data.min_price = min(today_prices)
            data.max_price = max(today_prices)

    ir.async_delete_issue(hass, DOMAIN, ISSUE_NORDPOOL_UNAVAILABLE)

    _LOGGER.debug(
        "Stored prices for local date(s) %s (Nord Pool request: %s)",
        sorted(all_by_date.keys()),
        date_str,
    )

    hass.bus.async_fire(
        f"{DOMAIN}_prices_updated",
        {"entry_id": entry.entry_id, "date": date_str},
    )

    if prices_are_new:
        data.auto_selected.add(date_str)
        await _auto_select_cheapest(hass, entry, date_str)

    return True


def _maybe_raise_unavailable_issue(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_NORDPOOL_UNAVAILABLE,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="nordpool_unavailable",
        translation_placeholders={},
    )


async def _daily_reset(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> None:
    """Prune schedules and prices older than yesterday."""
    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return

    yesterday = dt_util.now().date() - timedelta(days=1)
    data = entry.runtime_data

    for bucket in ("schedules", "prices"):
        prune_old_dates(getattr(data, bucket), yesterday)

    await _save_schedules(hass, entry)
    _LOGGER.debug("Midnight cleanup complete.")


async def _save_schedules(hass: HomeAssistant, entry: SpotSchedulerConfigEntry) -> None:
    """Persist schedules and prices to HA storage."""
    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return

    data = entry.runtime_data

    await data.store.async_save({
        "schedules": data.schedules,
        "prices": data.prices,
    })


async def _auto_select_cheapest(
    hass: HomeAssistant, entry: SpotSchedulerConfigEntry, date_str: str
) -> None:
    """Auto-configure hours after prices arrive."""
    if entry.entry_id not in hass.data.get(DOMAIN, set()):
        return

    data = entry.runtime_data
    merged = {**entry.data, **entry.options}

    prices = data.prices.get(date_str, {})
    if not prices:
        return

    devices = merged.get(CONF_DEVICES, [])
    schedules = data.schedules
    changed = False

    raw_auto = merged.get(CONF_AUTO_SELECT_HOURS, DEFAULT_AUTO_SELECT_HOURS)
    if isinstance(raw_auto, int):
        auto_hours: dict[str, int] = {d: raw_auto for d in devices}
    else:
        auto_hours = raw_auto if isinstance(raw_auto, dict) else {}

    start_hour = int(
        merged.get(
            CONF_AUTO_SELECT_START_HOUR,
            DEFAULT_AUTO_SELECT_START_HOUR,
        )
    )
    end_hour = int(
        merged.get(
            CONF_AUTO_SELECT_END_HOUR,
            DEFAULT_AUTO_SELECT_END_HOUR,
        )
    )

    start_hour = max(0, min(23, start_hour))
    end_hour = max(1, min(24, end_hour))

    auto_select_enabled = merged.get(
        CONF_AUTO_SELECT_ENABLED,
        DEFAULT_AUTO_SELECT_ENABLED,
    )

    for device_id in devices:
        if not auto_select_enabled:
            break

        if end_hour <= start_hour:
            _LOGGER.warning(
                "Auto-select skipped for %s: invalid time window %02d:00–%02d:00",
                device_id,
                start_hour,
                end_hour,
            )
            continue

        n = int(auto_hours.get(device_id, 0))
        if n <= 0:
            continue

        device_sched = schedules.get(date_str, {}).get(device_id, {})

        already_on = sum(
            1
            for hour_str, value in device_sched.items()
            if value is True and start_hour <= int(hour_str) < end_hour
        )

        remaining = max(0, n - already_on)
        if remaining <= 0:
            continue

        unset_prices = {
            h: p
            for h, p in prices.items()
            if str(h) not in device_sched
            and start_hour <= h < end_hour
        }

        if not unset_prices:
            continue

        cheap = cheapest_hours(unset_prices, remaining)

        schedules.setdefault(date_str, {}).setdefault(device_id, {})

        for hour in cheap:
            schedules[date_str][device_id][str(hour)] = True

        changed = True

        _LOGGER.info(
            "Auto-select: set %d cheapest hours for %s on %s within %02d:00–%02d:00 "
            "(%d already ON in window, %d added)",
            n,
            device_id,
            date_str,
            start_hour,
            end_hour,
            already_on,
            len(cheap),
        )

    if merged.get(CONF_BLOCK_EXPENSIVE_HOURS, DEFAULT_BLOCK_EXPENSIVE):
        exp_count = int(merged.get(CONF_EXPENSIVE_HOURS_COUNT, DEFAULT_EXPENSIVE_HOURS))

        if exp_count > 0:
            exp_hrs = expensive_hours(prices, exp_count)

            now = dt_util.now()
            today_str = now.date().isoformat()
            min_hour = (now.hour + 1) if date_str == today_str else 0

            blockable = [h for h in exp_hrs if h >= min_hour]

            for device_id in devices:
                for hour in blockable:
                    existing = schedules.get(date_str, {}).get(device_id, {}).get(str(hour))

                    if existing not in (True, "skip"):
                        set_schedule(schedules, date_str, device_id, hour, False)
                        changed = True

            _LOGGER.info(
                "Block expensive: set %d expensive hours (h>=%d) to OFF on %s",
                len(blockable),
                min_hour,
                date_str,
            )

    if changed:
        await _save_schedules(hass, entry)
        hass.bus.async_fire(f"{DOMAIN}_schedule_changed", {
            "device_id": None,
            "date": date_str,
            "hour": None,
            "enabled": None,
        })


def _register_services(hass: HomeAssistant) -> None:
    """Register services once; safe to call again on additional instances."""

    async def set_device_schedule(call: ServiceCall) -> None:
        target_date = (call.data.get("date") or dt_util.now().date()).isoformat()
        hour: int = call.data["hour"]
        device_id: str = call.data["device_id"]
        enabled: bool | str | None = call.data.get("enabled")

        matched = False

        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
            if cfg_entry.entry_id not in hass.data.get(DOMAIN, set()):
                continue

            data = cfg_entry.runtime_data
            devices = (
                cfg_entry.options.get(CONF_DEVICES)
                or cfg_entry.data.get(CONF_DEVICES, [])
            )

            if device_id not in devices:
                continue

            matched = True
            set_schedule(data.schedules, target_date, device_id, hour, enabled)
            await _save_schedules(hass, cfg_entry)

        if not matched:
            _LOGGER.warning(
                "set_device_schedule: device '%s' is not managed by any SpotScheduler entry. "
                "Check your configuration.",
                device_id,
            )
            return

        hass.bus.async_fire(f"{DOMAIN}_schedule_changed", {
            "device_id": device_id,
            "date": target_date,
            "hour": hour,
            "enabled": enabled,
        })

    async def refresh_prices(call: ServiceCall) -> None:
        target_date = call.data.get("date") or dt_util.now().date()

        if isinstance(target_date, str):
            target_date = date.fromisoformat(target_date)

        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
            if cfg_entry.entry_id not in hass.data.get(DOMAIN, set()):
                continue

            ok = await _fetch_prices_for_date(hass, cfg_entry, target_date)

            if not ok and target_date == dt_util.now().date():
                _maybe_raise_unavailable_issue(hass)

    async def apply_schedules_now(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        hass.bus.async_fire(f"{DOMAIN}_apply_now", {"entry_id": entry_id})

        _LOGGER.info(
            "apply_schedules_now triggered manually (entry_id=%s)",
            entry_id or "all",
        )

    async def run_auto_select(call: ServiceCall) -> None:
        """Manually run auto-select-cheapest and block-expensive-hours."""
        entry_id = call.data.get("entry_id")
        target_date = call.data.get("date") or dt_util.now().date()
        date_str = target_date.isoformat()

        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
            if cfg_entry.entry_id not in hass.data.get(DOMAIN, set()):
                continue

            if entry_id and cfg_entry.entry_id != entry_id:
                continue

            cfg_entry.runtime_data.auto_selected.add(date_str)
            await _auto_select_cheapest(hass, cfg_entry, date_str)

        _LOGGER.info(
            "run_auto_select triggered manually (entry_id=%s, date=%s)",
            entry_id or "all",
            date_str,
        )

    if not hass.services.has_service(DOMAIN, "set_device_schedule"):
        hass.services.async_register(
            DOMAIN,
            "set_device_schedule",
            set_device_schedule,
            schema=SET_SCHEDULE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, "refresh_prices"):
        hass.services.async_register(
            DOMAIN,
            "refresh_prices",
            refresh_prices,
            schema=REFRESH_PRICES_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, "apply_schedules_now"):
        hass.services.async_register(
            DOMAIN,
            "apply_schedules_now",
            apply_schedules_now,
            schema=APPLY_NOW_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, "run_auto_select"):
        hass.services.async_register(
            DOMAIN,
            "run_auto_select",
            run_auto_select,
            schema=RUN_AUTO_SELECT_SCHEMA,
        )
