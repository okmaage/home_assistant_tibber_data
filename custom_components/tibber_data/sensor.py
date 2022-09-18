"""Tibber data"""
import asyncio
import datetime
import logging
from typing import cast

import tibber
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import ENERGY_KILO_WATT_HOUR
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .helpers import get_historic_data, get_tibber_data, get_tibber_token

DOMAIN = "tibber_data"

_LOGGER = logging.getLogger(__name__)

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="peak_consumption",
        name="Average of 3 highest hourly consumption",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="monthly_avg_price",
        name="Monthly avg price",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="customer_avg_price",
        name="Monthly avg customer price",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="est_subsidy",
        name="Estimated subsidy",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="est_current_price_with_subsidy",
        name="Estimated price with subsidy",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="daily_cost_with_subsidy",
        name="Daily cost with subsidy",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="monthly_cost_with_subsidy",
        name="Monthly cost with subsidy",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="total_price_with_subsidy",
        name="Estimated total price with subsidy and grid price",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="grid_price",
        name="Grid price",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_platform(hass: HomeAssistant, _, async_add_entities, config):
    """Set up the Tibber sensor."""
    hass.data[DOMAIN] = {}
    dev = []
    tasks = []
    for home in hass.data["tibber"].get_homes(only_active=True):
        home = cast(tibber.TibberHome, home)
        if not home.info:
            await home.update_info()
        coordinator = TibberDataCoordinator(
            hass, home, config.get("email"), config.get("password")
        )
        tasks.append(coordinator.async_request_refresh())
        for entity_description in SENSORS:
            if (
                entity_description.key in ("daily_cost_with_subsidy",)
                and not home.has_real_time_consumption
            ):
                continue
            if entity_description.key in (
                "grid_price",
                "total_price_with_subsidy",
            ) and not config.get("password"):
                continue
            dev.append(TibberDataSensor(coordinator, entity_description))
    async_add_entities(dev)
    await asyncio.gather(*tasks)


class TibberDataSensor(SensorEntity, CoordinatorEntity["TibberDataCoordinator"]):
    """Representation of a Tibber sensor."""

    def __init__(self, coordinator, entity_description):
        """Initialize the sensor."""
        super().__init__(coordinator=coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.tibber_home.home_id}_{entity_description.key}"
        )
        if entity_description.native_unit_of_measurement is None:
            if entity_description.device_class == SensorDeviceClass.MONETARY:
                self._attr_native_unit_of_measurement = coordinator.tibber_home.currency
            else:
                self._attr_native_unit_of_measurement = (
                    coordinator.tibber_home.price_unit
                )
        self._attr_name = (
            f"{entity_description.name} {coordinator.tibber_home.address1}"
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.entity_description.key == "est_current_price_with_subsidy":
            price_data = self.coordinator.tibber_home.current_price_data()
            native_value = price_data[0] - self.coordinator.data.get("est_subsidy", 0)
        elif self.entity_description.key == "grid_price":
            native_value = (
                self.coordinator.data.get(self.entity_description.key, {}).get(
                    dt_util.now().replace(minute=0, second=0, microsecond=0)
                )
            )
        elif self.entity_description.key == "total_price_with_subsidy":
            grid_price = (
                self.coordinator.data.get("grid_price", {}).get(
                    dt_util.now().replace(minute=0, second=0, microsecond=0)
                )
            )
            price_data = self.coordinator.tibber_home.current_price_data()
            native_value = (
                grid_price + price_data[0] - self.coordinator.data.get("est_subsidy", 0)
            )
        else:
            native_value = self.coordinator.data.get(self.entity_description.key)

        self._attr_native_value = (
            round(native_value, 2) if native_value else native_value
        )
        if self.entity_description.key == "peak_consumption":
            self._attr_extra_state_attributes = self.coordinator.data.get(
                "peak_consumption_attrs"
            )

        self.async_write_ha_state()


class TibberDataCoordinator(DataUpdateCoordinator):
    """Handle Tibber data."""

    def __init__(self, hass, tibber_home: tibber.TibberHome, email: str, password: str):
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Tibber Data {tibber_home.name}",
            update_interval=datetime.timedelta(seconds=15),
        )
        self.tibber_home: tibber.TibberHome = tibber_home
        self.email = email
        self._password = password
        self._token = None
        _next_update = dt_util.now() - datetime.timedelta(minutes=1)
        self._update_functions = {
            self._get_data: _next_update,
        }
        if self._password:
            self._update_functions[self._get_data_tibber] = _next_update

    async def _async_update_data(self):
        """Update data via API."""
        now = dt_util.now(dt_util.DEFAULT_TIME_ZONE)
        data = {} if self.data is None else self.data
        for func, next_update in self._update_functions.copy().items():
            _LOGGER.debug("Updating Tibber data %s", next_update)
            if now >= next_update:
                self._update_functions[func] = await func(data, now)
        return data

    async def _get_data_tibber(self, data, now):
        """Update data via Tibber API."""
        session = async_get_clientsession(self.hass)
        if self._token is None:
            self._token = await get_tibber_token(session, self.email, self._password)
            if self._token is None:
                return now + datetime.timedelta(minutes=2)

        try:
            _data = await get_tibber_data(session, self._token)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Error fetching Tibber data")
            self._token = None
            return now + datetime.timedelta(minutes=2)

        for home in _data["data"]["me"]["homes"]:
            if home["id"] != self.tibber_home.home_id:
                continue
            data["grid_price"] = {}
            for price_info in home["subscription"]["priceRating"]["hourly"]["entries"]:
                data["grid_price"][
                    dt_util.parse_datetime(price_info["time"])
                ] = price_info["gridPrice"]

        if now.hour < 15:
            return now.replace(hour=15, minute=0, second=0, microsecond=0)
        return now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + datetime.timedelta(days=1)

    async def _get_data(self, data, now):
        """Get data from Tibber."""
        # pylint: disable=too-many-locals, too-many-branches, too-many-statements
        max_month = []
        cons_data = await get_historic_data(self.tibber_home, self.hass.data["tibber"])
        consumption_yesterday_available = False
        consumption_today_available = False
        month_consumption = set()

        for _hour in cons_data:
            _cons = _hour.get("consumption")
            if _cons is None:
                continue
            date = dt_util.parse_datetime(_hour.get("from"))
            if not (date.month == now.month and date.year == now.year):
                continue
            if date.date() == now.date() - datetime.timedelta(days=1):
                consumption_yesterday_available = True
            if date == now - datetime.timedelta(hours=1):
                consumption_today_available = True
            cons = Consumption(date, _cons, _hour.get("unitPrice"), _hour.get("cost"))
            month_consumption.add(cons)

            if len(max_month) < 3 or cons > max_month[-1]:
                same_day = False
                for k, _cons in enumerate(max_month):
                    if cons.day == _cons.day:
                        if cons > _cons:
                            max_month[k] = cons
                        same_day = True
                        break
                if not same_day:
                    max_month.append(cons)
                max_month.sort(reverse=True)
                if len(max_month) > 3:
                    del max_month[-1]
        if max_month:
            data["peak_consumption"] = sum(max_month) / len(max_month)
            data["peak_consumption_attrs"] = {
                "peak_consumption_dates": [x.timestamp for x in max_month],
                "peak_consumptions": [x.cons for x in max_month],
            }
        else:
            data["peak_consumption"] = None
            data["peak_consumption_attrs"] = None
        await self.tibber_home.update_price_info()

        if self.tibber_home.has_real_time_consumption:
            if consumption_today_available:
                next_update = (now + datetime.timedelta(hours=1)).replace(
                    minute=2, second=0, microsecond=0
                )
            else:
                next_update = now + datetime.timedelta(minutes=2)
        elif consumption_yesterday_available:
            next_update = (now + datetime.timedelta(days=1)).replace(
                hour=3, minute=0, second=0, microsecond=0
            )
        else:
            next_update = now + datetime.timedelta(minutes=15)

        prices_tomorrow_available = False
        for key, price in self.tibber_home.price_total.items():
            date = dt_util.parse_datetime(key)
            if date.date() == now.date() + datetime.timedelta(days=1):
                prices_tomorrow_available = True
            if not (date.month == now.month and date.year == now.year):
                continue
            month_consumption.add(Consumption(date, None, price, None))

        self.hass.data[DOMAIN][
            f"month_consumption_{self.tibber_home.home_id}"
        ] = month_consumption

        if prices_tomorrow_available:
            next_update = min(
                next_update,
                (now + datetime.timedelta(days=1)).replace(
                    hour=13, minute=0, second=0, microsecond=0
                ),
            )
        elif now.hour >= 13:
            next_update = min(next_update, now + datetime.timedelta(minutes=2))
        else:
            next_update = min(
                next_update,
                now.replace(hour=13, minute=1, second=0, microsecond=0),
            )

        _total_price = 0
        _n_price = 0
        total_price = 0
        n_price = 0
        total_cost = 0
        total_cost_day = 0
        total_cons = 0
        total_cons_day = 0
        for cons in month_consumption:
            _total_price += cons.price if cons.price else 0
            _n_price += 1 if cons.price else 0
            if cons.cost is None or cons.cons is None:
                continue
            total_price += cons.price if cons.price else 0
            n_price += 1 if cons.price else 0
            total_cost += cons.cost
            total_cons += cons.cons
            if cons.day == now.date():
                total_cost_day += cons.cost
                total_cons_day += cons.cons
        data["monthly_avg_price"] = total_price / n_price
        data["est_subsidy"] = (_total_price / _n_price - 0.7 * 1.25) * 0.9
        data["customer_avg_price"] = total_cost / total_cons

        data["daily_cost_with_subsidy"] = (
            total_cost_day - data["est_subsidy"] * total_cons_day
        )
        data["monthly_cost_with_subsidy"] = (
            total_cost - data["est_subsidy"] * total_cons
        )
        return next_update


class Consumption:
    """Consumption data."""

    def __init__(self, timestamp, cons, price, cost):
        """Initialize the data."""
        self.timestamp = timestamp
        self.cons = cons
        self.price = price
        self.cost = cost

    @property
    def day(self):
        """Return day."""
        return dt_util.as_local(self.timestamp).date()

    def __lt__(self, other):
        if self.cons is None and other.cons is None:
            return self.timestamp < other.timestamp
        if self.cons is None:
            return True
        if other.cons is None:
            return False
        return self.cons < other.cons

    def __eq__(self, other):
        return self.timestamp == other.timestamp

    def __hash__(self):
        return hash(self.timestamp)

    def __radd__(self, other):
        return other + self.cons

    def __str__(self):
        cons = f"{self.cons:.2f}" if self.cons else "-"
        cost = f"{self.cost:.2f}" if self.cost else "-"
        price = f"{self.price:.2f}" if self.price else "-"
        return f"Cons({self.timestamp}, {cons}, {price}, {cost})"

    def __repr__(self):
        return self.__str__()
