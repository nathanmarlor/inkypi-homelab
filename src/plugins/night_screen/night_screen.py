from plugins._theme.themed import ThemedPlugin


class NightScreen(ThemedPlugin):
    """A static, near-black screen for overnight hours.

    Deliberately contains nothing that changes between renders: InkyPi compares the
    rendered image hash with the last one and skips the panel refresh when identical,
    so the display goes dark once at the start of the night window and then stays
    still (no flashing) until the daytime playlist takes over.
    """

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        template_params = {
            "show_moon": settings.get("showMoon", "true") != "false",
            "plugin_settings": settings,
        }
        return self.render_image(dimensions, "night_screen.html", "night_screen.css", template_params)
