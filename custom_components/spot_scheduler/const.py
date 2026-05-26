"""Constants for SpotScheduler."""

DOMAIN = "spot_scheduler"
STORAGE_KEY = f"{DOMAIN}.schedules"
STORAGE_VERSION = 1

CONF_NORDPOOL_CONFIG_ENTRY = "nordpool_config_entry"
CONF_DEVICES = "devices"

# Expensive hours highlighting
CONF_EXPENSIVE_HOURS_COUNT = "expensive_hours_count"
DEFAULT_EXPENSIVE_HOURS = 3

# Auto-select cheapest hours
CONF_AUTO_SELECT_ENABLED = "auto_select_enabled"
DEFAULT_AUTO_SELECT_ENABLED = True

CONF_AUTO_SELECT_HOURS = "auto_select_hours"
DEFAULT_AUTO_SELECT_HOURS = 0   # 0 = disabled

# Time window for auto-select
# END hour is exclusive:
# start=0, end=7 => hours 00:00–06:59
CONF_AUTO_SELECT_START_HOUR = "auto_select_start_hour"
CONF_AUTO_SELECT_END_HOUR = "auto_select_end_hour"

DEFAULT_AUTO_SELECT_START_HOUR = 0
DEFAULT_AUTO_SELECT_END_HOUR = 24

# Price color thresholds (EUR/kWh cents)
# 0 means "use relative scaling"
CONF_PRICE_THRESHOLD_LOW = "price_threshold_low"
CONF_PRICE_THRESHOLD_HIGH = "price_threshold_high"

DEFAULT_PRICE_THRESHOLD_LOW = 5.0
DEFAULT_PRICE_THRESHOLD_HIGH = 15.0

NORDPOOL_DOMAIN = "nordpool"

# Issue registry identifiers
ISSUE_NORDPOOL_MISSING = "nordpool_integration_missing"
ISSUE_NORDPOOL_UNAVAILABLE = "nordpool_unavailable"

# How often to poll for tomorrow's prices when they haven't arrived yet.
# Core Nord Pool has no tomorrow_valid attribute,
# so we poll after ~13:00 local.
TOMORROW_POLL_INTERVAL_MINUTES = 15
TOMORROW_POLL_START_HOUR = 13

# Option: auto-set expensive hours to OFF when prices arrive
CONF_BLOCK_EXPENSIVE_HOURS = "block_expensive_hours"
DEFAULT_BLOCK_EXPENSIVE = False

# Option: what to do with hours that have no explicit schedule
# Values: "dont_touch" | "on" | "off"
CONF_DEFAULT_STATE = "default_state"
DEFAULT_DEFAULT_STATE = "dont_touch"
