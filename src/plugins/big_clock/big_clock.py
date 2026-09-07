from datetime import datetime

import pytz

from plugins._theme.themed import ThemedPlugin


class BigClock(ThemedPlugin):
    """Large, quiet clock in the same type family as the other screens.

    The panel refreshes every few minutes, so the time is shown to the minute with the
    refresh time underneath, which makes the inherent lag honest rather than confusing.
    """

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/London"))
        now = datetime.now(tz)
        twelve = device_config.get_config("time_format", default="24h") == "12h"

        time_text = now.strftime("%I:%M" if twelve else "%H:%M").lstrip("0") if twelve else now.strftime("%H:%M")
        day_of_year = int(now.strftime("%j"))
        days_in_year = 366 if now.year % 4 == 0 and (now.year % 100 != 0 or now.year % 400 == 0) else 365

        template_params = {
            "time": time_text,
            "ampm": now.strftime("%p").lower() if twelve else "",
            "weekday": now.strftime("%A"),
            "date": now.strftime("%-d %B %Y"),
            "week": int(now.strftime("%V")),
            "day_of_year": day_of_year,
            "days_left": days_in_year - day_of_year,
            "plugin_settings": settings,
        }
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return self.render_image(dimensions, "big_clock.html", "big_clock.css", template_params)
