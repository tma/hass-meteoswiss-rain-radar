from homeassistant.const import Platform

DOMAIN = "meteoswiss_rain_radar"
VERSION = "0.1.0"

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

DEFAULT_RADIUS = 5.0
DEFAULT_THRESHOLD = 0.2

CONF_RADIUS = "radius"
CONF_THRESHOLD = "threshold"

# Freshness and polling settings, matching the hail ranges and defaults.
CONF_RAIN_MAX_AGE = "rain_max_age_minutes"
CONF_RAIN_POLL = "rain_poll_seconds"

DEFAULT_RAIN_MAX_AGE = 10.0
DEFAULT_RAIN_POLL = 60.0

# Provisional reporting settings, independent of the legacy rain options.
CONF_HAIL_RADIUS = "hail_radius_km"
CONF_HAIL_THRESHOLD = "hail_poh_threshold"
CONF_HAIL_MAX_AGE = "hail_max_age_minutes"
CONF_HAIL_POLL = "hail_poll_seconds"

DEFAULT_HAIL_RADIUS = 10.0
DEFAULT_HAIL_THRESHOLD = 80.0
DEFAULT_HAIL_MAX_AGE = 10.0
DEFAULT_HAIL_POLL = 60.0
