"""Config flow for the Purolator parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
)
from .parcels import tracked_direction

_LOGGER = logging.getLogger(__name__)


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code upper-cased with separators stripped.

    Mirrors what a consumer site's own sanitiser does (uppercase, drop
    everything that is not ``A-Z0-9``), so codes pasted with spaces or dashes
    still work.
    """
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def valid_tracking_code(value: str) -> bool:
    """Accept every non-empty code.

    Real tracking-number shapes vary too much, and are not fully confirmed, to
    gate on a guess; an unrecognised code just comes back "not found" on the
    next poll.
    """
    return bool(value)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


class PurolatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Purolator integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PurolatorOptionsFlowHandler:
        """Return the options flow handler."""
        return PurolatorOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the Purolator hub — single instance, no input needed.

        Tracking is keyed on the tracking code alone (no account, no postal
        code), so there is nothing to ask at setup: the entry is created
        straight away and parcels are added afterwards via the options flow,
        the ``purolator.track_parcel`` service or a dashboard button.
        ``single_config_entry`` in the manifest enforces one hub.

        Tracking keys purely on the number — no postcode — so there is nothing
        more to collect here.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Purolator",
            data={},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )


class PurolatorOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels and integration settings from one menu.

    Incoming and outgoing parcels get a menu entry each, both opening the same
    list editor: this carrier is account-less and its payload cannot tell the
    two directions apart, so the user declares it by filing a code in one list
    or the other. Changes apply live via HA's options-update listener (which
    refreshes the coordinator), so new/removed per-parcel sensors appear and
    disappear immediately.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["incoming_parcels", "outgoing_parcels", "settings"],
        )

    async def async_step_incoming_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being received."""
        return await self._async_step_parcel_list(DIRECTION_INCOMING, user_input)

    async def async_step_outgoing_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being sent."""
        return await self._async_step_parcel_list(DIRECTION_OUTGOING, user_input)

    async def _async_step_parcel_list(
        self, direction: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show and handle one direction's complete tracked-code list.

        Both directions share a single ``CONF_PARCELS`` list, so a submission
        replaces this direction's entries and leaves the other direction's
        alone — except for a code submitted here that was filed the other way,
        which moves rather than being rejected: re-entering a code in the
        other list is how a user corrects a parcel they filed wrongly.
        """
        step_id = f"{direction}_parcels"
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = list(
                dict.fromkeys(
                    normalize_tracking_code(code)
                    for code in user_input.get("tracking_codes", [])
                    if normalize_tracking_code(code)
                )
            )
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                kept = [
                    parcel
                    for parcel in _current_parcels(self.config_entry)
                    if tracked_direction(parcel) != direction
                    and parcel.get(CONF_TRACKING_CODE) not in codes
                ]
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: kept
                        + [
                            {CONF_TRACKING_CODE: code, CONF_DIRECTION: direction}
                            for code in codes
                        ],
                    },
                )

        current_codes = [
            parcel[CONF_TRACKING_CODE]
            for parcel in _current_parcels(self.config_entry)
            if tracked_direction(parcel) == direction
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DELIVERED_FILTER_TYPE,
                        default=current.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["days", "parcels"],
                            translation_key=CONF_DELIVERED_FILTER_TYPE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_DELIVERED_FILTER_AMOUNT,
                        default=current.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_INCLUDE_HISTORY,
                        default=current.get(
                            CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
