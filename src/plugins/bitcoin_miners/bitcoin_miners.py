import logging
import math
from datetime import datetime

import pytz

from plugins._theme.themed import ThemedPlugin
from utils.homelab import Prom, fmt_si, local_setting

logger = logging.getLogger(__name__)

DEFAULT_PROMETHEUS_URL = local_setting("prometheus_url", "http://prometheus.local:9090")
TWO_32 = 2 ** 32

DEFAULT_NANO_URL = local_setting("bitaxe_url", "http://bitaxe.local")
NANO_NOMINAL_GHS = local_setting("bitaxe_nominal_ghs", 1000)


def parse_si(value):
    """Parse '296M' / '2.3G' style strings (or plain numbers) into a float."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    mult = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}
    if text and text[-1].upper() in mult:
        try:
            return float(text[:-1]) * mult[text[-1].upper()]
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_nano(url, timeout=8):
    """Read the Bitaxe Nano (ForgeOS / AxeOS style JSON API). Returns None when unreachable."""
    import requests
    try:
        resp = requests.get(f"{url.rstrip('/')}/api/system/info", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Bitaxe Nano unreachable at {url}: {e}")
        return None


def odds_text(p):
    """Turn a probability into '1 in N'."""
    if p <= 0:
        return "—"
    return f"1 in {fmt_si(1 / p, 1).replace('G', 'B').replace('T', 'T')}"


def fmt_pct(p):
    """Plain-decimal percentage with just enough precision to show two significant figures."""
    pct = 100 * p
    if pct <= 0:
        return "0%"
    if pct >= 10:
        return f"{pct:.0f}%"
    if pct >= 1:
        return f"{pct:.1f}%"
    digits = int(-math.floor(math.log10(pct))) + 1
    return f"{pct:.{min(digits, 9)}f}%"


def fmt_years(y):
    if y >= 1e6:
        return f"{y / 1e6:.1f}M yrs"
    if y >= 1000:
        return f"{y:,.0f} yrs"
    if y >= 1:
        return f"{y:.0f} yrs"
    return f"{y * 365:.0f} days"


def fmt_duration(seconds):
    if not seconds:
        return "—"
    d = seconds / 86400
    if d >= 1:
        return f"{d:.0f}d"
    return f"{seconds / 3600:.0f}h"


class BitcoinMiners(ThemedPlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["defaults"] = {
            "prometheusUrl": DEFAULT_PROMETHEUS_URL,
            "nanoUrl": DEFAULT_NANO_URL,
            "nanoNominalGhs": NANO_NOMINAL_GHS,
        }
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        prom = Prom(settings.get("prometheusUrl") or DEFAULT_PROMETHEUS_URL)
        if not prom.online():
            raise RuntimeError("Prometheus unreachable.")

        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/London"))
        now = datetime.now(tz)
        time_format = "%I:%M %p" if device_config.get_config("time_format", default="24h") == "12h" else "%H:%M"

        # ---- node ----
        difficulty = prom.scalar("bitcoin_difficulty")
        height = int(prom.scalar("bitcoin_latest_block_height"))
        if not difficulty or not height:
            raise RuntimeError("Bitcoin node metrics unavailable in Prometheus; keeping the previous image.")
        net_hashps = prom.scalar("bitcoin_hashps")
        fee_btc_kb = prom.scalar("bitcoin_est_smart_fee_2")
        node = {
            "online": prom.scalar('up{job="bitcoind"}') == 1,
            "height": height,
            "difficulty": difficulty,
            "net_hashrate": net_hashps,
            "peers": int(prom.scalar("bitcoin_peers")),
            "mempool": int(prom.scalar("bitcoin_mempool_size")),
            "mempool_mb": prom.scalar("bitcoin_mempool_bytes") / 1e6,
            "fee_sat_vb": fee_btc_kb * 1e8 / 1000,
            "sync_pct": 100 * prom.scalar("bitcoin_verification_progress"),
            "blocks_24h": int(prom.scalar("increase(bitcoin_latest_block_height[24h])")),
            "since_block_min": max(0, (datetime.now().timestamp() - prom.scalar("timestamp(bitcoin_latest_block_height)")) / 60),
        }
        node["subsidy"] = 50 / (2 ** (height // 210000)) if height else 3.125
        node["last_fees"] = prom.scalar("bitcoin_latest_block_fee")
        node["jackpot"] = node["subsidy"] + node["last_fees"]
        node["next_draw_min"] = max(0, 10 - node["since_block_min"])
        node["height_digits"] = list(f"{height:,}")

        # ---- miner (live from the Nano's own API) ----
        try:
            nominal = float(settings.get("nanoNominalGhs") or NANO_NOMINAL_GHS)
        except ValueError:
            nominal = NANO_NOMINAL_GHS
        info = fetch_nano(settings.get("nanoUrl") or DEFAULT_NANO_URL)
        live_ghs = float(info.get("hashRate") or 0) if info else 0.0
        nominal_ghs = nominal
        best_share = parse_si(info.get("bestDiff")) if info else 0.0
        online = bool(info) and live_ghs > 0
        pools = (info or {}).get("pools") or []
        pool_connected = any(p.get("connected") for p in pools) if pools else None
        power = float(info.get("power") or 0) if info else 0.0
        miners = [{
            "name": (info or {}).get("hostname") or "Bitaxe Nano",
            "model": f'{(info or {}).get("ASICModel", "")} x{(info or {}).get("asicCount", "")}'.strip(" x"),
            "online": online,
            "hashrate_ths": live_ghs / 1000,
            "nominal_ths": nominal / 1000,
            "temp": float(info.get("temp") or 0) if info else None,
            "vr_temp": float(info.get("vrTemp") or 0) if info else None,
            "power": power,
            "efficiency": (power / (live_ghs / 1000)) if live_ghs else 0.0,
            "accepted": int(info.get("sharesAccepted") or 0) if info else None,
            "rejected": int(info.get("sharesRejected") or 0) if info else None,
            "best": fmt_si(best_share) if best_share else "—",
            "best_session": fmt_si(parse_si(info.get("bestSessionDiff"))) if info and info.get("bestSessionDiff") else None,
            "uptime": fmt_duration(float(info.get("uptimeSeconds") or 0)) if info else None,
            "frequency": int(info.get("frequency") or 0) if info else None,
            "fan_rpm": int(info.get("fanrpm") or 0) if info else None,
            "pool": (info or {}).get("stratumURL", ""),
            "pool_connected": pool_connected,
            "version": (info or {}).get("version", ""),
        }]

        using_nominal = live_ghs <= 0
        fleet_ghs = nominal_ghs if using_nominal else live_ghs
        fleet_hs = fleet_ghs * 1e9

        # Expected blocks per day at this hashrate: H * seconds / (D * 2^32)
        lam_day = fleet_hs * 86400 / (difficulty * TWO_32) if difficulty else 0.0
        p_day = 1 - math.exp(-lam_day) if lam_day else 0.0
        p_year = 1 - math.exp(-lam_day * 365) if lam_day else 0.0
        years_per_block = (1 / (lam_day * 365)) if lam_day else 0.0
        remaining_day = (now.replace(hour=23, minute=59, second=59) - now).total_seconds() / 86400
        p_rest_of_day = 1 - math.exp(-lam_day * remaining_day) if lam_day else 0.0

        odds = {
            "fleet_ths": fleet_ghs / 1000,
            "using_nominal": using_nominal,
            "p_day": p_day,
            "p_day_text": fmt_pct(p_day),
            "p_rest_text": fmt_pct(p_rest_of_day),
            "odds_day": odds_text(p_day),
            "p_year_text": fmt_pct(p_year),
            "years_per_block": years_per_block,
            "wait_text": fmt_years(years_per_block) if years_per_block else "—",
            "network_odds": odds_text(fleet_hs / net_hashps) if net_hashps and fleet_hs else "—",
            "network_share": (fleet_hs / net_hashps) if net_hashps else 0.0,
            "best_share": fmt_si(best_share) if best_share else "—",
            "best_vs_network": (best_share / difficulty) if difficulty else 0.0,
            "best_vs_text": fmt_pct(best_share / difficulty) if (difficulty and best_share) else "—",
            "difficulty_text": fmt_si(difficulty, 1),
        }

        template_params = {
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "node": node,
            "miners": miners,
            "odds": odds,
            "fmt_si": fmt_si,
            "fmt_pct": fmt_pct,
            "plugin_settings": settings,
        }
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return self.render_image(dimensions, "bitcoin_miners.html", "bitcoin_miners.css", template_params)
