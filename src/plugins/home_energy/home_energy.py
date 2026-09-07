import logging
from datetime import datetime, timedelta

import pytz

from plugins._theme.themed import ThemedPlugin
from utils.homelab import HomeAssistant, bucket_series, local_setting

logger = logging.getLogger(__name__)

DEFAULT_HA_URL = local_setting("home_assistant_url", "http://homeassistant.local:8123")

# Default entity ids (FoxESS Modbus, Solcast, myenergi, Octopus integrations). Override any of them
# with an "energy_entities" object in src/config/local_settings.json.
E = {
    "pv_now": "sensor.pv_power_now",                         # kW
    "solar_today": "sensor.solar_energy_today",              # kWh
    "battery_soc": "sensor.battery_soc",                     # %
    "battery_charge": "sensor.battery_charge",               # kW
    "battery_discharge": "sensor.battery_discharge",         # kW
    "battery_charge_today": "sensor.battery_charge_today",   # kWh
    "battery_discharge_today": "sensor.battery_discharge_today",
    "battery_temp": "sensor.battery_temp",
    "load": "sensor.load_power",                             # kW
    "grid_import": "sensor.grid_consumption",                # kW
    "grid_export": "sensor.feed_in",                         # kW
    "grid_import_today": "sensor.grid_consumption_energy_today",
    "grid_export_today": "sensor.feed_in_energy_today",
    "inverter_state": "sensor.inverter_state",
    "forecast_today": "sensor.solcast_pv_forecast_forecast_today",
    "forecast_remaining": "sensor.solcast_pv_forecast_forecast_remaining_today",
    "forecast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "forecast_peak_today": "sensor.solcast_pv_forecast_peak_forecast_today",   # W
    "forecast_peak_time": "sensor.solcast_pv_forecast_peak_time_today",
    "import_rate": "sensor.octopus_energy_electricity_current_rate",
    "export_rate": "sensor.octopus_energy_electricity_export_current_rate",
    "zappi_status": "sensor.myenergi_zappi_status",
    "zappi_plug": "sensor.myenergi_zappi_plug_status",
    "zappi_session": "sensor.myenergi_zappi_charge_added_session",
    "eddi_status": "sensor.myenergi_eddi_status",
    "eddi_tank": "sensor.myenergi_eddi_temp_tank_1",
    "eddi_today": "sensor.myenergi_eddi_energy_used_today",
    "rack_power": "sensor.server_rack_power",  # W
}
E.update(local_setting("energy_entities", {}))


class HomeEnergy(ThemedPlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {"required": True, "service": "Home Assistant", "expected_key": "HOME_ASSISTANT_TOKEN"}
        template_params["defaults"] = {"haUrl": DEFAULT_HA_URL}
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        token = device_config.load_env_key("HOME_ASSISTANT_TOKEN")
        if not token:
            raise RuntimeError("Home Assistant token not configured (HOME_ASSISTANT_TOKEN).")
        ha = HomeAssistant(settings.get("haUrl") or DEFAULT_HA_URL, token)

        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/London"))
        now = datetime.now(tz)
        time_format = "%I:%M %p" if device_config.get_config("time_format", default="24h") == "12h" else "%H:%M"

        try:
            ha.fetch(list(E.values()))
        except Exception as e:
            logger.error(f"Home Assistant unreachable: {e}")
            raise RuntimeError("Could not reach Home Assistant.")

        n = ha.num
        pv_now = n(E["pv_now"])
        solar_today = n(E["solar_today"])
        forecast_today = n(E["forecast_today"])
        forecast_remaining = n(E["forecast_remaining"])
        charge, discharge = n(E["battery_charge"]), n(E["battery_discharge"])
        imp, exp = n(E["grid_import"]), n(E["grid_export"])

        if charge > 0.05:
            battery_state, battery_kw = "charging", charge
        elif discharge > 0.05:
            battery_state, battery_kw = "discharging", discharge
        else:
            battery_state, battery_kw = "idle", 0.0

        if imp > 0.05:
            grid_state, grid_kw = "importing", imp
        elif exp > 0.05:
            grid_state, grid_kw = "exporting", exp
        else:
            grid_state, grid_kw = "balanced", 0.0

        expected_total = solar_today + forecast_remaining
        self_sufficiency = None
        load_today_est = solar_today - n(E["grid_export_today"]) + n(E["grid_import_today"]) + n(E["battery_discharge_today"]) - n(E["battery_charge_today"])
        if load_today_est > 0:
            self_sufficiency = max(0, min(100, 100 * (1 - n(E["grid_import_today"]) / load_today_est)))

        peak_time = ""
        raw_peak = ha.raw(E["forecast_peak_time"])
        if raw_peak:
            try:
                peak_time = datetime.fromisoformat(raw_peak.replace("Z", "+00:00")).astimezone(tz).strftime(time_format).lstrip("0")
            except ValueError:
                pass

        # Today's curves from HA history, 15-minute buckets.
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = midnight + timedelta(days=1)
        buckets = 96
        try:
            hist = ha.history([E["pv_now"], E["battery_soc"], E["load"]], midnight, now)
        except Exception as e:
            logger.warning(f"HA history failed: {e}")
            hist = {}
        pv_series = bucket_series(hist.get(E["pv_now"], []), midnight, end, buckets)
        soc_series = bucket_series(hist.get(E["battery_soc"], []), midnight, end, buckets)
        load_series = bucket_series(hist.get(E["load"], []), midnight, end, buckets)
        peak_kw = max([v for v in pv_series + load_series if v is not None] + [n(E["forecast_peak_today"]) / 1000, 1.0])

        template_params = {
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "inverter_state": ha.raw(E["inverter_state"], "?"),
            "pv_now": pv_now,
            "solar_today": solar_today,
            "forecast_today": forecast_today,
            "forecast_remaining": forecast_remaining,
            "forecast_tomorrow": n(E["forecast_tomorrow"]),
            "forecast_pct": min(100, int(100 * solar_today / forecast_today)) if forecast_today else 0,
            "expected_total": expected_total,
            "peak_time": peak_time,
            "soc": int(n(E["battery_soc"])),
            "battery_state": battery_state,
            "battery_kw": battery_kw,
            "battery_in_today": n(E["battery_charge_today"]),
            "battery_out_today": n(E["battery_discharge_today"]),
            "battery_temp": n(E["battery_temp"]),
            "grid_state": grid_state,
            "grid_kw": grid_kw,
            "import_today": n(E["grid_import_today"]),
            "export_today": n(E["grid_export_today"]),
            "load_now": n(E["load"]),
            "self_sufficiency": self_sufficiency,
            "import_rate_p": 100 * n(E["import_rate"]),
            "export_rate_p": 100 * n(E["export_rate"]),
            "zappi_status": ha.raw(E["zappi_status"], "?"),
            "zappi_plug": {"EV Disconnected": "unplugged", "EV Connected": "plugged in", "Charging": "charging"}.get(ha.raw(E["zappi_plug"], ""), (ha.raw(E["zappi_plug"], "") or "").lower()),
            "zappi_session": n(E["zappi_session"]),
            "eddi_status": ha.raw(E["eddi_status"], "?"),
            "eddi_tank": n(E["eddi_tank"]),
            "eddi_today": n(E["eddi_today"]),
            "rack_power": n(E["rack_power"]),
            "chart": self.chart(pv_series, load_series, soc_series, peak_kw, now, midnight),
            "plugin_settings": settings,
        }

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return self.render_image(dimensions, "home_energy.html", "home_energy.css", template_params)

    @staticmethod
    def chart(pv, load, soc, peak_kw, now, midnight):
        """Build SVG path data for a 24h chart in a 0..1000 x 0..100 coordinate space."""
        w, h = 1000.0, 100.0
        n = len(pv)

        def x(i):
            return w * i / (n - 1)

        def y_kw(v):
            return h - 0.94 * h * min(v, peak_kw) / peak_kw

        def poly(series, yfn):
            pts = [(x(i), yfn(v)) for i, v in enumerate(series) if v is not None]
            return " ".join(f"{px:.1f},{py:.1f}" for px, py in pts), pts

        pv_line, pv_pts = poly(pv, y_kw)
        load_line, _ = poly(load, y_kw)
        soc_line, _ = poly(soc, lambda v: h - 0.94 * h * v / 100)
        area = ""
        if pv_pts:
            area = f"{pv_pts[0][0]:.1f},{h} " + pv_line + f" {pv_pts[-1][0]:.1f},{h}"
        now_x = w * (now - midnight).total_seconds() / 86400
        return {"pv_area": area, "pv_line": pv_line, "load_line": load_line, "soc_line": soc_line, "now_x": now_x, "peak_kw": peak_kw}
