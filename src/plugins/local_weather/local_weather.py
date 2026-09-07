import logging
from datetime import datetime

import pytz
import requests

from plugins._theme.themed import ThemedPlugin

logger = logging.getLogger(__name__)

FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,relative_humidity_2m,is_day,precipitation"
    "&hourly=weather_code,temperature_2m,precipitation_probability,precipitation,is_day"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"
    "&timezone={tz}&forecast_days=7&{units}"
)
UNIT_PARAMS = {
    "metric": "temperature_unit=celsius&wind_speed_unit=mph&precipitation_unit=mm",
    "imperial": "temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch",
}

# WMO weather interpretation codes -> (icon id, short description)
WMO = {
    0: ("sun", "Clear"),
    1: ("sun-cloud", "Mostly clear"),
    2: ("sun-cloud", "Partly cloudy"),
    3: ("cloud", "Overcast"),
    45: ("fog", "Fog"),
    48: ("fog", "Freezing fog"),
    51: ("drizzle", "Light drizzle"),
    53: ("drizzle", "Drizzle"),
    55: ("drizzle", "Heavy drizzle"),
    56: ("sleet", "Freezing drizzle"),
    57: ("sleet", "Freezing drizzle"),
    61: ("rain", "Light rain"),
    63: ("rain", "Rain"),
    65: ("rain-heavy", "Heavy rain"),
    66: ("sleet", "Freezing rain"),
    67: ("sleet", "Freezing rain"),
    71: ("snow", "Light snow"),
    73: ("snow", "Snow"),
    75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("showers", "Light showers"),
    81: ("showers", "Showers"),
    82: ("rain-heavy", "Heavy showers"),
    85: ("snow", "Snow showers"),
    86: ("snow", "Snow showers"),
    95: ("thunder", "Thunderstorm"),
    96: ("thunder", "Thunder & hail"),
    99: ("thunder", "Thunder & hail"),
}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def describe(code, is_day=True):
    icon, text = WMO.get(int(code or 0), ("cloud", "Cloudy"))
    if not is_day and icon in ("sun", "sun-cloud"):
        icon = "moon" if icon == "sun" else "moon-cloud"
    return icon, text


def compass(deg):
    return COMPASS[int(((deg or 0) + 22.5) // 45) % 8]


class LocalWeather(ThemedPlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        try:
            lat = float((settings.get("latitude") or "").strip())
            lon = float((settings.get("longitude") or "").strip())
        except ValueError:
            raise RuntimeError("Latitude and longitude are required.")
        units = settings.get("units") if settings.get("units") in UNIT_PARAMS else "metric"
        title = (settings.get("title") or "").strip() or "Weather"
        try:
            hours = max(6, min(int(settings.get("hourlyHours") or 12), 16))
        except ValueError:
            hours = 12

        tz_name = device_config.get_config("timezone", default="Europe/London")
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        time_format = "%I:%M %p" if device_config.get_config("time_format", default="24h") == "12h" else "%H:%M"

        url = FORECAST_URL.format(lat=lat, lon=lon, tz=tz_name.replace("/", "%2F"), units=UNIT_PARAMS[units])
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Open-Meteo request failed: {e}")
            raise RuntimeError("Could not reach Open-Meteo.")

        cur = data["current"]
        hourly = data["hourly"]
        daily = data["daily"]

        icon, text = describe(cur["weather_code"], cur.get("is_day", 1))
        current = {
            "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]),
            "icon": icon,
            "text": text,
            "wind": round(cur["wind_speed_10m"]),
            "wind_dir": compass(cur.get("wind_direction_10m")),
            "humidity": round(cur.get("relative_humidity_2m") or 0),
        }

        # Hourly strip: from the current hour forward.
        start = next((i for i, t in enumerate(hourly["time"]) if t >= now.strftime("%Y-%m-%dT%H:00")), 0)
        hour_rows = []
        for i in range(start, min(start + hours, len(hourly["time"]))):
            h_icon, _ = describe(hourly["weather_code"][i], hourly["is_day"][i])
            t = datetime.fromisoformat(hourly["time"][i])
            hour_rows.append({
                "label": t.strftime("%H") if time_format.startswith("%H") else t.strftime("%-I%p").lower(),
                "icon": h_icon,
                "temp": round(hourly["temperature_2m"][i]),
                "rain": int(hourly["precipitation_probability"][i] or 0),
                "amount": hourly["precipitation"][i] or 0,
            })
        temps = [h["temp"] for h in hour_rows] or [0]
        t_min, t_max = min(temps), max(temps)
        span = max(t_max - t_min, 4)
        for h in hour_rows:
            h["temp_pct"] = int(100 * (h["temp"] - t_min) / span)

        day_rows = []
        for i, day in enumerate(daily["time"]):
            d = datetime.fromisoformat(day)
            d_icon, d_text = describe(daily["weather_code"][i])
            day_rows.append({
                "name": "Today" if i == 0 else d.strftime("%a"),
                "icon": d_icon,
                "text": d_text,
                "high": round(daily["temperature_2m_max"][i]),
                "low": round(daily["temperature_2m_min"][i]),
                "rain": int(daily["precipitation_probability_max"][i] or 0),
            })

        sunrise = datetime.fromisoformat(daily["sunrise"][0]).strftime(time_format).lstrip("0")
        sunset = datetime.fromisoformat(daily["sunset"][0]).strftime(time_format).lstrip("0")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        if not cur.get("is_day", 1):
            panel = "ink"
        elif icon in ("sun", "sun-cloud"):
            panel = "yellow"
        else:
            panel = "blue"

        rain_ahead = max([h["rain"] for h in hour_rows] + [0])

        template_params = {
            "rain_ahead": rain_ahead,
            "panel": panel,
            "title": title,
            "current": current,
            "today": day_rows[0],
            "hours": hour_rows,
            "days": day_rows[1:6],
            "sunrise": sunrise,
            "sunset": sunset,
            "unit": "°C" if units == "metric" else "°F",
            "wind_unit": "mph",
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "plugin_settings": settings,
        }
        return self.render_image(dimensions, "local_weather.html", "local_weather.css", template_params)
