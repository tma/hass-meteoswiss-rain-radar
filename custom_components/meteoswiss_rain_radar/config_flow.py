from __future__ import annotations

import math

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section

from .const import (
    CONF_HAIL_MAX_AGE,
    CONF_HAIL_POLL,
    CONF_HAIL_RADIUS,
    CONF_HAIL_THRESHOLD,
    CONF_RADIUS,
    CONF_RAIN_MAX_AGE,
    CONF_RAIN_POLL,
    CONF_THRESHOLD,
    DEFAULT_HAIL_MAX_AGE,
    DEFAULT_HAIL_POLL,
    DEFAULT_HAIL_RADIUS,
    DEFAULT_HAIL_THRESHOLD,
    DEFAULT_RADIUS,
    DEFAULT_RAIN_MAX_AGE,
    DEFAULT_RAIN_POLL,
    DEFAULT_THRESHOLD,
    DOMAIN,
)

# Key, default, minimum, maximum. Rain and hail share the freshness/poll bounds.
RAIN_BOUNDED = (
    (CONF_RAIN_MAX_AGE, DEFAULT_RAIN_MAX_AGE, 1, 60),
    (CONF_RAIN_POLL, DEFAULT_RAIN_POLL, 15, 300),
)
HAIL_BOUNDED = (
    (CONF_HAIL_RADIUS, DEFAULT_HAIL_RADIUS, 0.1, 100),
    (CONF_HAIL_THRESHOLD, DEFAULT_HAIL_THRESHOLD, 0, 100),
    (CONF_HAIL_MAX_AGE, DEFAULT_HAIL_MAX_AGE, 1, 60),
    (CONF_HAIL_POLL, DEFAULT_HAIL_POLL, 15, 300),
)


class SwissRainRadarConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        schema = settings_schema({})
        if user_input is not None:
            try:
                settings = validate_settings(schema, user_input)
            except vol.Invalid:
                errors["base"] = "invalid_options"
            else:
                return self.async_create_entry(
                    title="MeteoSwiss Rain Radar",
                    data={
                        **settings["rain"],
                        "latitude": self.hass.config.latitude,
                        "longitude": self.hass.config.longitude,
                    },
                    options=settings["hail"],
                )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


def _finite(value):
    if not math.isfinite(value):
        raise vol.Invalid("Value must be finite")
    return value


def hail_options_schema(options, *, include_finite=True):
    """Bound provisional settings; omit the callable only for form rendering."""
    return _bounded_schema(options, HAIL_BOUNDED, include_finite=include_finite)


def rain_options_schema(options, *, include_finite=True):
    """Keep the legacy radius/threshold keys, add bounded freshness/poll keys."""
    return {
        vol.Optional(
            CONF_RADIUS, default=options.get(CONF_RADIUS, DEFAULT_RADIUS)
        ): vol.Coerce(float),
        vol.Optional(
            CONF_THRESHOLD, default=options.get(CONF_THRESHOLD, DEFAULT_THRESHOLD)
        ): vol.Coerce(float),
        **_bounded_schema(options, RAIN_BOUNDED, include_finite=include_finite),
    }


def _bounded_schema(options, fields, *, include_finite=True):
    finite = [_finite] if include_finite else []
    return {
        vol.Optional(key, default=options.get(key, default)): vol.All(
            vol.Coerce(float), *finite, vol.Range(min=minimum, max=maximum)
        )
        for key, default, minimum, maximum in fields
    }


def settings_schema(options):
    """Use HA-native sections for display, keeping persisted keys flat."""
    return vol.Schema(
        {
            vol.Optional("rain", default=dict): section(
                vol.Schema(rain_options_schema(options, include_finite=False)),
                {"collapsed": False},
            ),
            vol.Optional("hail", default=dict): section(
                vol.Schema(hail_options_schema(options, include_finite=False)),
                {"collapsed": False},
            ),
        }
    )


def validate_settings(schema, user_input):
    """Reject nonfinite settings even on versions where Range allows NaN."""
    settings = schema(user_input)
    for values in settings.values():
        for value in values.values():
            _finite(value)
    return settings


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        schema = settings_schema(
            {**self.config_entry.data, **self.config_entry.options}
        )
        if user_input is not None:
            try:
                settings = validate_settings(schema, user_input)
            except vol.Invalid:
                errors["base"] = "invalid_options"
            else:
                return self.async_create_entry(
                    title="", data={**settings["rain"], **settings["hail"]}
                )

        # Reuse saved/default values, not invalid submissions (NaN breaks JSON).
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
