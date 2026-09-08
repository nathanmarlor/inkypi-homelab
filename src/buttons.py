"""Physical button shortcuts for Inky Impression boards.

The Impression has four tactile buttons on the back wired to GPIO 5, 6, 16 and 24
(A to D, top to bottom; the 13.3" board uses 25 instead of 16). Each button can be
mapped in the device config to a playlist plugin instance which is displayed
immediately when the button is pressed:

    "buttons": {
        "A": {"playlist": "Default", "plugin_id": "ai_usage", "plugin_instance": "AI Usage"},
        "B": {"playlist": "Default", "plugin_id": "bbc_news", "plugin_instance": "BBC News"}
    }

The listener is a daemon thread using the gpiod v2 API. If gpiod or the GPIO chip is
unavailable (dev machines, non-Pi hosts) it logs once and does nothing.
"""

import logging
import threading
import time
from datetime import timedelta

from refresh_task import PlaylistRefresh

logger = logging.getLogger(__name__)

BUTTON_PINS = {"A": 5, "B": 6, "C": 16, "D": 24}
DEBOUNCE_SECONDS = 0.4
STALE_EVENT_SECONDS = 2.0  # presses that queued while a refresh was running are dropped
CONSUMER = "inkypi-buttons"


class ButtonListener:
    def __init__(self, device_config, refresh_task, pins=None):
        self.device_config = device_config
        self.refresh_task = refresh_task
        self.pins = pins or BUTTON_PINS
        self.thread = None
        self.running = False
        self.request = None
        self.last_press = {}

    def start(self):
        try:
            self.request = self._request_lines()
        except Exception as e:
            logger.info(f"Button shortcuts disabled: {e}")
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, name="buttons", daemon=True)
        self.thread.start()
        logger.info(f"Listening for buttons on GPIO {self.pins}")

    def stop(self):
        self.running = False
        if self.request:
            try:
                self.request.release()
            except Exception:
                pass

    def _request_lines(self):
        import gpiod
        from gpiod.line import Bias, Direction, Edge

        try:
            import gpiodevice
            chip = gpiodevice.find_chip_by_platform()
        except Exception:
            chip = gpiod.Chip("/dev/gpiochip0")

        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            bias=Bias.PULL_UP,
            edge_detection=Edge.FALLING,
            debounce_period=timedelta(milliseconds=50),
        )
        offsets = {chip.line_offset_from_id(f"GPIO{pin}"): label for label, pin in self.pins.items()}
        self.offsets = offsets
        return chip.request_lines(consumer=CONSUMER, config=dict.fromkeys(offsets, settings))

    def _run(self):
        while self.running:
            try:
                if not self.request.wait_edge_events(timedelta(seconds=1)):
                    continue
                for event in self.request.read_edge_events():
                    self._on_event(event)
            except Exception:
                logger.exception("Button listener error")
                time.sleep(1)

    def _on_event(self, event):
        label = self.offsets.get(event.line_offset)
        if not label:
            return
        now = time.monotonic()
        if now - event.timestamp_ns / 1e9 > STALE_EVENT_SECONDS:
            logger.debug(f"Ignoring stale press of button {label}")
            return
        if now - self.last_press.get(label, 0) < DEBOUNCE_SECONDS:
            return
        self.last_press[label] = now
        self.press(label)

    def press(self, label):
        """Displays the plugin instance mapped to the button, if any. Safe to call from any thread."""
        mapping = (self.device_config.get_config("buttons", default={}) or {}).get(label)
        if not mapping:
            logger.info(f"Button {label} pressed, no shortcut configured")
            return
        playlist_manager = self.device_config.get_playlist_manager()
        playlist = playlist_manager.get_playlist(mapping.get("playlist"))
        instance = playlist.find_plugin(mapping.get("plugin_id"), mapping.get("plugin_instance")) if playlist else None
        if not instance:
            logger.warning(f"Button {label} shortcut points at a missing playlist entry: {mapping}")
            return
        logger.info(f"Button {label} pressed, showing {instance.plugin_id}/{instance.name}")
        try:
            self.refresh_task.manual_update(PlaylistRefresh(playlist, instance, force=True))
        except Exception as e:
            logger.error(f"Button {label} refresh failed: {e}")
