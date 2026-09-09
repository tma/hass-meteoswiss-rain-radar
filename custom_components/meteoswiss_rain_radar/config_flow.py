from __future__ import annotations

import math

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_HAIL_MAX_AGE,
    CONF_HAIL_POLL,
    CONF_HAIL_RADIUS,
    CONF_HAIL_THRESHOLD,
    CONF_RADIUS,
    CONF_THRESHOLD,
    DEFAULT_HAIL_MAX_AGE,
    DEFAULT_HAIL_POLL,
    DEFAULT_HAIL_RADIUS,
    DEFAULT_HAIL_THRESHOLD,
    DEFAULT_RADIUS,
    DEFAULT_THRESHOLD,
    DOMAIN,
)


class SwissRainRadarConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            user_input["latitude"] = self.hass.config.latitude
            user_input["longitude"] = self.hass.config.longitude

            return self.async_create_entry(
                title="MeteoSwiss Rain Radar",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RADIUS,
                    default=DEFAULT_RADIUS,
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_THRESHOLD,
                    default=DEFAULT_THRESHOLD,
                ): vol.Coerce(float),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


def _finite(value):
    if not math.isfinite(value):
        raise vol.Invalid("Value must be finite")
    return value


def hail_options_schema(options):
    """Bound provisional settings independently of the existing rain defaults."""
    return {
        vol.Optional(key, default=options.get(key, default)): vol.All(
            vol.Coerce(float), _finite, vol.Range(min=minimum, max=maximum)
        )
        for key, default, minimum, maximum in (
            (CONF_HAIL_RADIUS, DEFAULT_HAIL_RADIUS, 0.1, 100),
            (CONF_HAIL_THRESHOLD, DEFAULT_HAIL_THRESHOLD, 0, 100),
            (CONF_HAIL_MAX_AGE, DEFAULT_HAIL_MAX_AGE, 1, 60),
            (CONF_HAIL_POLL, DEFAULT_HAIL_POLL, 15, 300),
        )
    }


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RADIUS,
                    default=self.config_entry.options.get(
                        CONF_RADIUS,
                        self.config_entry.data[CONF_RADIUS],
                    ),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_THRESHOLD,
                    default=self.config_entry.options.get(
                        CONF_THRESHOLD,
                        self.config_entry.data[CONF_THRESHOLD],
                    ),
                ): vol.Coerce(float),
                **hail_options_schema(self.config_entry.options),
            }
        )
        if user_input is not None:
            try:
                user_input = schema(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_options"
            else:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
