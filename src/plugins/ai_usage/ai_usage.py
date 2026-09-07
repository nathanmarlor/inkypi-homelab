import logging
from datetime import datetime, timedelta

import pytz
import requests

from plugins._theme.themed import ThemedPlugin
from utils.homelab import local_setting

logger = logging.getLogger(__name__)

DEFAULT_PROMETHEUS_URL = local_setting("prometheus_url", "http://prometheus.local:9090")
DEFAULT_GPU_INSTANCE = local_setting("gpu_instance", "gpu-host")
DEFAULT_USAGE_URL = local_setting("usage_url", "src/config/usage.json")
DEFAULT_BUSY_THRESHOLD = 5      # % GPU busy that counts as "occupied"
DEFAULT_ELECTRICITY_PENCE = 24.5
DEFAULT_GATEWAY_URL = local_setting("litellm_url", "http://litellm.local:4000")


def fmt_tokens(n):
    n = float(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 100_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def fmt_money(v, symbol="$"):
    v = float(v or 0)
    if v >= 100:
        return f"{symbol}{v:,.0f}"
    return f"{symbol}{v:,.2f}"


def short_model(name):
    name = (name or "").replace("claude-", "").replace("-GGUF", "")
    name = name.replace("Qwen3.8-Flash-Next-IQ4_XS", "Qwen3.8 Flash")
    for token in ("-UD-Q4_K_XL", "-UD-Q6_K_XL", "-MTP", "-IQ4_XS", "-IQ2XXS", "-IQ2_M", "-Q4_K_M"):
        name = name.replace(token, "")
    return name[:22]


class AiUsage(ThemedPlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["defaults"] = {
            "prometheusUrl": DEFAULT_PROMETHEUS_URL,
            "gpuInstance": DEFAULT_GPU_INSTANCE,
            "usageUrl": DEFAULT_USAGE_URL,
            "busyThreshold": DEFAULT_BUSY_THRESHOLD,
            "electricityPence": DEFAULT_ELECTRICITY_PENCE,
            "gatewayUrl": DEFAULT_GATEWAY_URL,
        }
        template_params["api_key"] = {"required": False, "service": "LiteLLM gateway", "expected_key": "LITELLM_MASTER_KEY"}
        template_params["style_settings"] = True
        return template_params

    # ---------- Prometheus ----------

    def prom_query(self, base, query, timeout=10):
        resp = requests.get(f"{base.rstrip('/')}/api/v1/query", params={"query": query}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {query}")
        return data["data"]["result"]

    def prom_scalar(self, base, query, default=0.0):
        try:
            result = self.prom_query(base, query)
            return float(result[0]["value"][1]) if result else default
        except Exception as e:
            logger.warning(f"Prometheus scalar failed for {query}: {e}")
            return default

    def prom_by_label(self, base, query, label):
        try:
            rows = []
            for r in self.prom_query(base, query):
                rows.append((r["metric"].get(label, "?"), float(r["value"][1])))
            return sorted(rows, key=lambda kv: -kv[1])
        except Exception as e:
            logger.warning(f"Prometheus by-label failed for {query}: {e}")
            return []

    def prom_range(self, base, query, start, end, step):
        try:
            resp = requests.get(
                f"{base.rstrip('/')}/api/v1/query_range",
                params={"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()["data"]["result"]
            return [(float(t), float(v)) for t, v in result[0]["values"]] if result else []
        except Exception as e:
            logger.warning(f"Prometheus range failed for {query}: {e}")
            return []

    def collect_m5(self, settings, now):
        base = settings.get("prometheusUrl") or DEFAULT_PROMETHEUS_URL
        inst = settings.get("gpuInstance") or DEFAULT_GPU_INSTANCE
        try:
            threshold = float(settings.get("busyThreshold") or DEFAULT_BUSY_THRESHOLD)
        except ValueError:
            threshold = DEFAULT_BUSY_THRESHOLD
        try:
            pence = float(settings.get("electricityPence") or DEFAULT_ELECTRICITY_PENCE)
        except ValueError:
            pence = DEFAULT_ELECTRICITY_PENCE

        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = max(int((now - midnight).total_seconds()), 300)
        w = f"{since}s"
        hours = since / 3600
        gpu = f'amdgpu_gpu_busy_percent{{instance="{inst}"}}'

        # Reachability check: if this fails the whole panel is marked offline.
        try:
            self.prom_query(base, "up")
            online = True
        except Exception as e:
            logger.error(f"Prometheus unreachable: {e}")
            online = False

        m5 = {"online": online}
        if not online:
            return m5

        m5["spend_today"] = self.prom_scalar(base, f"sum(increase(litellm_spend_metric_total[{w}]))")
        m5["spend_total"] = self.prom_scalar(base, "sum(litellm_spend_metric_total)")
        # the counter only goes back to the gateway's last restart, so say since when
        started = self.prom_scalar(base, 'process_start_time_seconds{job="litellm"}')
        m5["spend_since"] = datetime.fromtimestamp(started, tz=now.tzinfo).strftime("%-d %b") if started else "restart"
        m5["tokens_today"] = self.prom_scalar(base, f"sum(increase(litellm_total_tokens_metric_total[{w}]))")
        m5["requests_today"] = self.prom_scalar(base, f"sum(increase(lemonade_model_requests_total[{w}]))")

        m5["gpu_now"] = self.prom_scalar(base, gpu)
        m5["gpu_avg"] = self.prom_scalar(base, f"avg_over_time({gpu}[{w}])")
        m5["occupied_pct"] = 100 * self.prom_scalar(base, f"avg_over_time(({gpu} > bool {threshold})[{w}:15s])")
        m5["occupied_hours"] = hours * m5["occupied_pct"] / 100
        m5["threshold"] = threshold

        mem_used = self.prom_scalar(base, f'amdgpu_gtt_used_bytes{{instance="{inst}"}}')
        mem_total = self.prom_scalar(base, f'amdgpu_gtt_total_bytes{{instance="{inst}"}}', default=1)
        m5["mem_used_gb"] = mem_used / 2**30
        m5["mem_total_gb"] = mem_total / 2**30
        m5["mem_pct"] = 100 * mem_used / mem_total if mem_total else 0

        m5["power_now"] = self.prom_scalar(base, f'sum(node_hwmon_power_watt{{instance="{inst}"}})')
        avg_power = self.prom_scalar(base, f'avg_over_time(sum(node_hwmon_power_watt{{instance="{inst}"}})[{w}:30s])')
        m5["kwh_today"] = avg_power * hours / 1000
        m5["electricity_gbp"] = m5["kwh_today"] * pence / 100
        m5["loaded_models"] = int(self.prom_scalar(base, "sum(lemonade_loaded_models)"))

        m5["by_client"] = [
            (name, v) for name, v in self.prom_by_label(base, f"sum by (api_key_alias)(increase(litellm_spend_metric_total[{w}]))", "api_key_alias")
            if v > 0.0005
        ][:4]
        m5["by_model"] = [
            (short_model(name), v) for name, v in self.prom_by_label(base, f"sum by (model)(increase(litellm_spend_metric_total[{w}]))", "model")
            if v > 0.0005
        ][:3]
        top_client = m5["by_client"][0][1] if m5["by_client"] else 0
        m5["client_max"] = max(top_client, 0.01)
        m5["model_max"] = max((m5["by_model"][0][1] if m5["by_model"] else 0), 0.01)

        # Hourly GPU busy for today's bar strip, one bar per hour, 24 slots.
        series = self.prom_range(base, f"avg_over_time({gpu}[1h])", midnight + timedelta(hours=1), midnight + timedelta(hours=24), 3600)
        by_hour = {}
        for ts, v in series:
            hour = int((datetime.fromtimestamp(ts, tz=now.tzinfo) - midnight).total_seconds() // 3600) - 1
            if 0 <= hour < 24:
                by_hour[hour] = v
        peak = max([v for v in by_hour.values()] + [0])
        axis_max = 100  # GPU busy is a percentage; a fixed scale needs no caption and never misleads
        m5["hour_axis_max"] = axis_max
        m5["hourly"] = [{"hour": h, "value": by_hour.get(h), "pct": 100 * (by_hour.get(h) or 0) / axis_max, "future": h > now.hour} for h in range(24)]
        return m5

    # ---------- LiteLLM gateway lifetime totals (its own database, not Prometheus) ----------

    def collect_gateway(self, settings, device_config):
        key = device_config.load_env_key("LITELLM_MASTER_KEY")
        base = (settings.get("gatewayUrl") or DEFAULT_GATEWAY_URL).rstrip("/")
        if not key:
            return {"available": False}
        headers = {"Authorization": f"Bearer {key}"}
        try:
            # LiteLLM's daily aggregate table outlives the request logs and carries spend, tokens
            # and requests for every day since the gateway went live, so all three share one window.
            resp = requests.get(
                f"{base}/user/daily/activity/aggregated", headers=headers,
                params={"start_date": "2024-01-01", "end_date": "2099-12-31"}, timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            meta = data.get("metadata") or {}
            dates = sorted(r.get("date", "") for r in (data.get("results") or []) if isinstance(r, dict) and r.get("date"))
            since = datetime.strptime(dates[0], "%Y-%m-%d").strftime("%-d %b") if dates else ""
            return {
                "available": True,
                "spend": float(meta.get("total_spend") or 0),
                "tokens": int(meta.get("total_tokens") or 0),
                "requests": int(meta.get("total_api_requests") or 0),
                "since": since,
                "days": len(dates),
            }
        except Exception as e:
            logger.warning(f"Gateway totals unavailable: {e}")
            return {"available": False}

    # ---------- Claude plan limits, read directly with the device's own long-lived token ----------

    CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
    CLAUDE_TOKEN_URLS = ["https://platform.claude.com/v1/oauth/token", "https://console.anthropic.com/v1/oauth/token"]
    CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client

    def claude_access_token(self):
        """Read the credentials written by `claude auth login` on this device, refreshing them when near expiry.

        Access tokens last about eight hours; Claude Code only refreshes them while it is running, so the
        plugin does the refresh itself and writes the rotated tokens back in the same file format.
        """
        import json, os, time
        path = os.path.expanduser(local_setting("claude_credentials", "~/.claude/.credentials.json"))
        try:
            with open(path) as fh:
                full = json.load(fh)
            creds = full["claudeAiOauth"]
        except (OSError, KeyError, ValueError):
            return None, f"no Claude login at {path}"
        if creds.get("expiresAt", 0) / 1000 - time.time() > 600:
            return creds["accessToken"], None
        body = {"grant_type": "refresh_token", "refresh_token": creds.get("refreshToken"), "client_id": self.CLAUDE_CLIENT_ID}
        for url in self.CLAUDE_TOKEN_URLS:
            try:
                resp = requests.post(url, json=body, timeout=15, headers={"User-Agent": "inkypi-ai-usage"})
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"Claude token refresh via {url} failed: {e}")
                continue
            creds["accessToken"] = data["access_token"]
            creds["refreshToken"] = data.get("refresh_token", creds.get("refreshToken"))
            creds["expiresAt"] = int(time.time() * 1000) + int(data.get("expires_in", 28800)) * 1000
            if data.get("scope"):
                creds["scopes"] = data["scope"].split()
            full["claudeAiOauth"] = creds
            owner = os.stat(path)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(full, fh)
            os.chmod(tmp, 0o600)
            try:
                os.chown(tmp, owner.st_uid, owner.st_gid)  # the service runs as root; keep the file readable by the user who logged in
            except PermissionError:
                pass
            os.replace(tmp, path)
            logger.info("Claude credentials refreshed")
            return creds["accessToken"], None
        return None, "Claude token expired and refresh failed; run `claude auth login` on the device"

    def collect_limits_direct(self, device_config):
        """Same figures as Claude Code's /usage screen, using this device's own Claude login."""
        token, problem = self.claude_access_token()
        if not token:
            return {"available": False, "reason": problem} if problem and "no Claude login" not in problem else None
        try:
            resp = requests.get(self.CLAUDE_USAGE_URL, timeout=15, headers={
                "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "User-Agent": "inkypi-ai-usage",
            })
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Claude usage endpoint failed: {e}")
            return {"available": False, "reason": f"limits request failed ({e.__class__.__name__})"}
        limits = []
        for lim in data.get("limits") or []:
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            kind = lim.get("kind")
            label = "Session" if kind == "session" else "Weekly · all models" if kind == "weekly_all" else f"Weekly · {model}" if model else (kind or "Limit")
            limits.append({"kind": kind, "label": label, "percent": lim.get("percent"), "severity": lim.get("severity"), "resets_at": lim.get("resets_at")})
        tier = ""
        try:
            import json, os
            tier_creds = json.load(open(os.path.expanduser(local_setting("claude_credentials", "~/.claude/.credentials.json"))))["claudeAiOauth"]
            tier = (tier_creds.get("subscriptionType") or "").title() + (" 20x" if "20x" in (tier_creds.get("rateLimitTier") or "") else " 5x" if "5x" in (tier_creds.get("rateLimitTier") or "") else "")
        except Exception:
            pass
        return {"available": True, "plan": tier.strip(), "limits": limits}

    # ---------- Claude Code / Codex exporter (optional: token and cost figures) ----------

    def collect_usage(self, settings, tz, device_config):
        direct = self.collect_limits_direct(device_config)
        url = settings.get("usageUrl") or DEFAULT_USAGE_URL
        try:
            if url.startswith("http://") or url.startswith("https://"):
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            else:
                import json
                with open(url) as fh:
                    data = json.load(fh)
        except Exception as e:
            logger.info(f"No exporter snapshot at {url} ({e.__class__.__name__}); showing plan limits only")
            data = {}
            if direct is None:
                return {"online": False}

        # How old is this snapshot? Pushed data goes stale if the source machine is off.
        age_text = ""
        stale = False
        try:
            generated = datetime.fromisoformat(data.get("generated_at", "").replace("Z", "+00:00"))
            age_min = (datetime.now(generated.tzinfo) - generated).total_seconds() / 60
            stale = age_min > 20
            if stale:
                age_text = f"as of {generated.astimezone(tz).strftime('%a %H:%M')}"
        except (ValueError, AttributeError):
            pass

        now = datetime.now(tz)

        def reset_text(iso):
            if not iso:
                return ""
            try:
                at = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                return ""
            delta = at - now
            if delta.total_seconds() <= 0:
                return "resets now"
            if delta < timedelta(hours=24):
                mins = int(delta.total_seconds() // 60)
                return f"resets in {mins // 60}h {mins % 60:02d}m"
            return f"resets {at.strftime('%a %H:%M')}"

        def shape(section):
            today = section.get("today", {})
            daily = section.get("daily", [])
            limits = dict(direct or section.get("limits") or {"available": False, "reason": "no limits"})
            if limits.get("available"):
                limits["limits"] = [
                    {**lim, "percent": int(round(lim.get("percent") or 0)), "reset_text": reset_text(lim.get("resets_at"))}
                    for lim in limits.get("limits", [])
                ]
            max_cost = max([d.get("cost_usd", 0) for d in daily] + [0.01])
            return {
                "available": section.get("available", False),
                "limits": limits,
                "today": today,
                "by_model": [(short_model(m["model"]), m["cost_usd"]) for m in today.get("by_model", [])[:2]],
                "daily": [
                    {"label": datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a")[0], "cost": d.get("cost_usd", 0), "pct": 100 * d.get("cost_usd", 0) / max_cost}
                    for d in daily
                ],
                "week_cost": sum(d.get("cost_usd", 0) for d in daily),
            }

        claude = shape(data.get("claude", {}))
        claude["has_stats"] = bool(data.get("claude", {}).get("available")) and not stale
        return {"online": True, "stale": stale, "age_text": age_text, "host": data.get("host", ""), "claude": claude, "codex": shape(data.get("codex", {}))}

    # ---------- render ----------

    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/London"))
        now = datetime.now(tz)
        time_format = "%I:%M %p" if device_config.get_config("time_format", default="24h") == "12h" else "%H:%M"

        m5 = self.collect_m5(settings, now)
        usage = self.collect_usage(settings, tz, device_config)
        gateway = self.collect_gateway(settings, device_config)

        if not m5.get("online") and not usage.get("online"):
            raise RuntimeError("Neither Prometheus nor the usage exporter could be reached.")

        template_params = {
            "m5": m5,
            "gateway": gateway,
            "usage": usage,
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "fmt_tokens": fmt_tokens,
            "fmt_money": fmt_money,
            "plugin_settings": settings,
        }
        return self.render_image(dimensions, "ai_usage.html", "ai_usage.css", template_params)
