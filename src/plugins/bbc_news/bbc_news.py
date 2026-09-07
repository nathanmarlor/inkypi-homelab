import html
import logging
import re
from datetime import datetime

import feedparser
import pytz
import requests

from plugins._theme.themed import ThemedPlugin

logger = logging.getLogger(__name__)

FEEDS = [
    {"id": "top", "name": "Top Stories", "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    {"id": "uk", "name": "UK", "url": "https://feeds.bbci.co.uk/news/uk/rss.xml"},
    {"id": "world", "name": "World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"id": "politics", "name": "Politics", "url": "https://feeds.bbci.co.uk/news/politics/rss.xml"},
    {"id": "business", "name": "Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"id": "technology", "name": "Technology", "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
    {"id": "science", "name": "Science & Environment", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    {"id": "health", "name": "Health", "url": "https://feeds.bbci.co.uk/news/health/rss.xml"},
    {"id": "entertainment", "name": "Entertainment & Arts", "url": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"},
    {"id": "sport", "name": "Sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
]
FEEDS_BY_ID = {f["id"]: f for f in FEEDS}

STORY_COUNTS = [3, 4, 5]
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)


def fetch_article_text(url, max_chars, timeout=6):
    """Pull the opening paragraphs of a BBC article so the screen has more than the feed's one-liner."""
    try:
        resp = requests.get(url.split("?")[0], timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (InkyPi)"})
        resp.raise_for_status()
        body = resp.content.decode("utf-8", errors="replace")
    except requests.RequestException as e:
        logger.warning(f"Article fetch failed for {url}: {e}")
        return ""
    paras = []
    for raw in PARA_RE.findall(body):
        text = clean_text(raw)
        if len(text) < 60 or text.startswith(("Watch:", "Follow ", "Sign up", "Get in touch")):
            continue
        paras.append(text)
        if sum(len(p) for p in paras) >= max_chars:
            break
    out = " ".join(paras)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


def clean_text(value):
    """Strip tags and entities from feed text and collapse whitespace."""
    text = html.unescape(TAG_RE.sub(" ", value or ""))
    return WS_RE.sub(" ", text).strip()


class BbcNews(ThemedPlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["feeds"] = FEEDS
        template_params["story_counts"] = STORY_COUNTS
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        feed = FEEDS_BY_ID.get(settings.get("feed") or "top", FEEDS_BY_ID["top"])

        try:
            story_count = int(settings.get("storyCount") or 4)
        except ValueError:
            story_count = 4
        story_count = min(max(story_count, min(STORY_COUNTS)), max(STORY_COUNTS))

        show_lead_image = settings.get("showLeadImage") == "true"

        stories = self.fetch_stories(feed["url"])
        if not stories:
            raise RuntimeError("BBC feed returned no stories.")
        # Enrich the stories that will be shown with the opening of the article itself.
        for i, story in enumerate(stories[:story_count]):
            text = fetch_article_text(story["link"], 520 if i == 0 else 330)
            if text and len(text) > len(story["snippet"]):
                story["snippet"] = text

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/London"))
        now = datetime.now(tz)
        time_format = "%I:%M %p" if device_config.get_config("time_format", default="24h") == "12h" else "%H:%M"

        template_params = {
            "section": feed["name"],
            "stories": stories[:story_count],
            "story_count": story_count,
            "show_lead_image": show_lead_image,
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "bbc_news.html", "bbc_news.css", template_params)

    def fetch_stories(self, url, timeout=10):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (InkyPi)"})
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch BBC feed {url}: {e}")
            raise RuntimeError("Could not reach the BBC News feed.")

        parsed = feedparser.parse(resp.content)
        stories = []
        for entry in parsed.entries:
            title = clean_text(entry.get("title"))
            if not title:
                continue
            image = None
            thumbs = entry.get("media_thumbnail") or []
            if thumbs:
                image = thumbs[0].get("url")
            stories.append({
                "title": title,
                "snippet": clean_text(entry.get("summary") or entry.get("description")),
                "image": image,
                "link": entry.get("link", ""),
            })
        return stories
