"""Physical button shortcuts for Inky Impression boards.

The Impression has four tactile buttons on the back wired to GPIO 5, 6, 16 and 24
(A to D, top to bottom; the 13.3" board uses 25 instead of 16). Each button can be
mapped in the device config to a playlist plugin instance which is displayed
immediately when the button is pressed:

    "buttons": {
        "A": {"playlist": "Default", "plugin_id": "ai_usage", "plugin_instance": "AI Usage"},
        "B": {"playlist": "Default", "plugin_id": "bbc_news", "plugin_instance": "BBC News"},
        "C": {"action": "previous"},
        "D": {"action": "next"}
    }

"previous" and "next" step through the currently active playlist and move its position,
so the scheduler carries on from the screen you stepped to.

The listener is a daemon thread using the gpiod v2 API. If gpiod or the GPIO chip is
unavailable (dev machines, non-Pi hosts) it logs once and does nothing.

Feedback: the panel takes ~30 s to redraw and cannot show anything sooner, so the Pi's
green activity LED (next to the buttons) blinks fast from the moment of the press until
the panel has finished. Presses made while a refresh is running are not lost: the most
recent one is acted on once the refresh completes.
"""

import logging
import threading
import time
from datetime import timedelta

from refresh_task import PlaylistRefresh

logger = logging.getLogger(__name__)

BUTTON_PINS = {"A": 5, "B": 6, "C": 16, "D": 24}
DEBOUNCE_SECONDS = 0.4
CONSUMER = "inkypi-buttons"
LED_PATH = "/sys/class/leds/ACT"


class ActivityLed:
    """Fast-blinks the Pi's activity LED while a button press is being handled."""

    def __init__(self, path=LED_PATH):
        self.path = path
        self.previous_trigger = None

    def _write(self, name, value):
        with open(f"{self.path}/{name}", "w") as f:
            f.write(str(value))

    def blink(self):
        try:
            with open(f"{self.path}/trigger") as f:
                current = f.read()
            if self.previous_trigger is None:
                self.previous_trigger = current[current.index("[") + 1:current.index("]")]
            self._write("trigger", "timer")
            self._write("delay_on", 100)
            self._write("delay_off", 100)
        except Exception as e:
            logger.debug(f"Activity LED unavailable: {e}")

    def restore(self):
        if self.previous_trigger is None:
            return
        try:
            self._write("trigger", self.previous_trigger)
        except Exception as e:
            logger.debug(f"Activity LED restore failed: {e}")


class ButtonListener:
    def __init__(self, device_config, refresh_task, pins=None):
        self.device_config = device_config
        self.refresh_task = refresh_task
        self.pins = pins or BUTTON_PINS
        self.thread = None
        self.running = False
        self.request = None
        self.last_press = {}
        self.led = ActivityLed()

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
                # Events that queued while a press was being handled arrive as a batch:
                # act on the most recent one only, so the last button pressed wins.
                events = list(self.request.read_edge_events())
                if events:
                    self._on_event(events[-1])
            except Exception:
                logger.exception("Button listener error")
                time.sleep(1)

    def _on_event(self, event):
        label = self.offsets.get(event.line_offset)
        if not label:
            return
        pressed_at = event.timestamp_ns / 1e9
        if pressed_at - self.last_press.get(label, 0) < DEBOUNCE_SECONDS:
            return
        self.last_press[label] = pressed_at
        self.press(label)

    def press(self, label):
        """Runs the shortcut mapped to the button, if any. Safe to call from any thread."""
        mapping = (self.device_config.get_config("buttons", default={}) or {}).get(label)
        if not mapping:
            logger.info(f"Button {label} pressed, no shortcut configured")
            return
        playlist_manager = self.device_config.get_playlist_manager()
        action = mapping.get("action")
        if action in ("next", "previous"):
            playlist = playlist_manager.determine_active_playlist(self.refresh_task._get_current_datetime())
            if not playlist or not playlist.plugins:
                logger.info(f"Button {label} pressed, no active playlist to step through")
                return
            step = 1 if action == "next" else -1
            index = ((playlist.current_plugin_index or 0) + step) % len(playlist.plugins)
            instance = playlist.plugins[index]
        else:
            playlist = playlist_manager.get_playlist(mapping.get("playlist"))
            instance = playlist.find_plugin(mapping.get("plugin_id"), mapping.get("plugin_instance")) if playlist else None
            if not instance:
                logger.warning(f"Button {label} shortcut points at a missing playlist entry: {mapping}")
                return
            index = playlist.plugins.index(instance)
        logger.info(f"Button {label} pressed, showing {instance.plugin_id}/{instance.name}")
        self.led.blink()
        try:
            self.refresh_task.manual_update(PlaylistRefresh(playlist, instance, force=True))
            # keep the playlist position in step with what is on the panel
            playlist.current_plugin_index = index
            self.device_config.write_config()
        except Exception as e:
            logger.error(f"Button {label} refresh failed: {e}")
        finally:
            self.led.restore()
