"""Mixin that injects the shared theme stylesheet between the base plugin CSS and the plugin's own CSS."""
import os

from plugins.base_plugin.base_plugin import BASE_PLUGIN_RENDER_DIR, STATIC_DIR, BasePlugin
from utils.app_utils import get_fonts
from utils.image_utils import take_screenshot_html

THEME_CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.css")


class ThemedPlugin(BasePlugin):
    def render_image(self, dimensions, html_file, css_file=None, template_params={}):
        css_files = [os.path.join(BASE_PLUGIN_RENDER_DIR, "plugin.css"), THEME_CSS]
        if css_file:
            css_files.append(os.path.join(self.render_dir, css_file))

        template_params["style_sheets"] = css_files
        template_params["width"] = dimensions[0]
        template_params["height"] = dimensions[1]
        template_params["font_faces"] = get_fonts()
        template_params["static_dir"] = STATIC_DIR

        template = self.env.get_template(html_file)
        return take_screenshot_html(template.render(template_params), dimensions)
