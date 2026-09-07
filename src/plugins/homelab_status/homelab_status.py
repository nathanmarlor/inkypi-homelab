import logging
from datetime import datetime

import pytz

from plugins._theme.themed import ThemedPlugin
from utils.homelab import Prom, local_setting

logger = logging.getLogger(__name__)

DEFAULT_PROMETHEUS_URL = local_setting("prometheus_url", "http://prometheus.local:9090")

# Friendly names for node_exporter instances, keyed by Prometheus instance label.
# Set "host_names" in local_settings.json; with none configured every host-metrics instance is shown by its label.
HOST_NAMES = local_setting("host_names", {})
HOST_JOB = local_setting("host_metrics_job", "host-metrics")
TARGET_NAMES = {
    "asicos-nano": "Bitaxe Nano",
    "nerdqaxe-exporter": "NerdQAxe",
    "bitcoind": "Bitcoin node",
    "lemonade": "Lemonade",
    "litellm": "LiteLLM",
    "kubelet": "k3s node",
    "host-metrics": "host",
    "traefik-dash-svc": "Traefik",
    "monitoring/flux-system": "Flux",
}


class HomelabStatus(ThemedPlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["defaults"] = {"prometheusUrl": DEFAULT_PROMETHEUS_URL}
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        prom = Prom(settings.get("prometheusUrl") or DEFAULT_PROMETHEUS_URL)
        if not prom.online():
            raise RuntimeError("Prometheus unreachable.")

        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/London"))
        now = datetime.now(tz)
        time_format = "%I:%M %p" if device_config.get_config("time_format", default="24h") == "12h" else "%H:%M"

        # ---- scrape targets ----
        targets = prom.rows("up")
        total = len(targets)
        down = []
        for m, v in targets:
            if v != 1:
                name = TARGET_NAMES.get(m.get("job"), m.get("job", "?"))
                if m.get("job") in ("kubelet", "host-metrics"):
                    name += f" {HOST_NAMES.get(m.get('instance'), m.get('instance', ''))}"
                down.append(name)

        # ---- k3s nodes ----
        pods = {m.get("node"): v for m, v in prom.rows("kubelet_running_pods")}
        containers = {m.get("node"): v for m, v in prom.rows("kubelet_running_containers")}
        cpu = {m.get("instance"): v for m, v in prom.rows('sum by (instance)(rate(container_cpu_usage_seconds_total{id="/"}[5m]))')}
        cores = {m.get("node"): (m.get("instance"), v) for m, v in prom.rows("machine_cpu_cores")}
        mem = {m.get("node"): v for m, v in prom.rows('container_memory_working_set_bytes{id="/"}')}
        nodes = []
        for node in sorted(pods):
            inst, ncores = cores.get(node, (None, 0))
            used = cpu.get(inst, 0.0)
            nodes.append({
                "name": node.replace("k3s-prod-", ""),
                "pods": int(pods.get(node, 0)),
                "containers": int(containers.get(node, 0)),
                "cpu_cores": used,
                "cpu_pct": int(100 * used / ncores) if ncores else 0,
                "mem_gb": mem.get(node, 0) / 2**30,
            })

        # ---- hosts (node_exporter) ----
        load = {m.get("instance"): v for m, v in prom.rows('node_load1{job="' + HOST_JOB + '"}')}
        cpu_pct = {m.get("instance"): v for m, v in prom.rows('100 * (1 - avg by (instance)(rate(node_cpu_seconds_total{job="' + HOST_JOB + '",mode="idle"}[5m])))')}
        cpus = {m.get("instance"): v for m, v in prom.rows('count by (instance)(node_cpu_seconds_total{job="' + HOST_JOB + '",mode="idle"})')}
        mem_pct = {m.get("instance"): v for m, v in prom.rows('100 * (1 - node_memory_MemAvailable_bytes{job="' + HOST_JOB + '"} / node_memory_MemTotal_bytes)')}
        mem_total = {m.get("instance"): v for m, v in prom.rows('node_memory_MemTotal_bytes{job="' + HOST_JOB + '"}')}
        disk_pct = {m.get("instance"): v for m, v in prom.rows('100 * (1 - node_filesystem_avail_bytes{job="' + HOST_JOB + '",mountpoint="/"} / node_filesystem_size_bytes{job="' + HOST_JOB + '",mountpoint="/"})')}
        temp = {m.get("instance"): v for m, v in prom.rows('max by (instance)(node_hwmon_temp_celsius{job="' + HOST_JOB + '"})')}
        hosts = []
        host_keys = list(HOST_NAMES) or sorted(load)
        for inst in host_keys:
            if inst not in load:
                hosts.append({"name": HOST_NAMES.get(inst, inst), "online": False})
                continue
            hosts.append({
                "name": HOST_NAMES.get(inst, inst),
                "online": True,
                "load": load[inst],
                "load_pct": int(100 * load[inst] / cpus[inst]) if cpus.get(inst) else 0,
                "cpu_pct": int(cpu_pct.get(inst, 0)),
                "cores": int(cpus.get(inst, 0)),
                "mem_pct": int(mem_pct.get(inst, 0)),
                "mem_gb": mem_total.get(inst, 0) / 2**30,
                "disk_pct": int(disk_pct.get(inst, 0)),
                "temp": temp.get(inst),
            })

        # ---- services ----
        flux_ready = prom.scalar('sum(gotk_reconcile_condition{type="Ready",status="True"})')
        flux_failed = prom.scalar('sum(gotk_reconcile_condition{type="Ready",status="False"})')
        flux_failed_names = [m.get("name", "?") for m, v in prom.rows('gotk_reconcile_condition{type="Ready",status="False"} == 1')]
        req_min = prom.scalar("sum(rate(traefik_service_requests_total[15m]))*60")
        err_min = prom.scalar('sum(rate(traefik_service_requests_total{code=~"5.."}[15m]))*60')
        pvc_used = prom.scalar("sum(kubelet_volume_stats_used_bytes)")
        pvc_cap = prom.scalar("sum(kubelet_volume_stats_capacity_bytes)")
        pvc_full = [
            (m.get("persistentvolumeclaim", "?"), v)
            for m, v in prom.rows("100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes > 85")
        ]
        node_sync = prom.scalar("bitcoin_verification_progress")
        lemonade_models = int(prom.scalar("sum(lemonade_loaded_models)"))
        m5_gfx_temp = prom.scalar("max(m5_gfx_temp_celsius)")
        m5_throttle = prom.scalar("sum(increase(m5_throttle_residency_total[24h]))")
        m5_thermal = prom.scalar('sum(increase(m5_throttle_residency_total{reason=~"thm_.*|prochot"}[24h]))')

        ns_mem = [(m.get("namespace", "?"), v / 2**30) for m, v in prom.rows('topk(6, sum by (namespace)(container_memory_working_set_bytes{container!="",namespace!=""}))')]
        ns_mem.sort(key=lambda kv: -kv[1])
        ns_max = max([v for _, v in ns_mem] + [0.1])
        pvc_rows = [(m.get("persistentvolumeclaim", "?"), m.get("namespace", ""), v) for m, v in prom.rows("topk(5, 100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes)")]
        fullest = {}
        for n, _, v in pvc_rows:
            fullest[n] = max(fullest.get(n, 0), v)
        pvc_top = sorted(fullest.items(), key=lambda kv: -kv[1])[:5]

        cluster = {
            "pods": int(sum(pods.values())),
            "nodes_ready": int(prom.scalar('count(up{job="kubelet"} == 1)')),
            "nodes_total": int(prom.scalar('count(up{job="kubelet"})')),
            "cores_used": prom.scalar('sum(rate(container_cpu_usage_seconds_total{id="/"}[5m]))'),
            "cores_total": int(prom.scalar("sum(machine_cpu_cores)")),
            "containers": int(sum(containers.values())),
        }

        tiles = [
            {"label": "Scrape targets", "value": f"{total - len(down)}/{total}", "sub": ", ".join(down) if down else "all up", "bad": bool(down)},
            {"label": "Flux", "value": f"{int(flux_ready)}/{int(flux_ready + flux_failed)}", "sub": ", ".join(flux_failed_names) if flux_failed_names else "reconciled", "bad": flux_failed > 0},
            {"label": "Traefik", "value": f"{req_min:.0f}", "sub": (f"req/min, {err_min:.1f} 5xx/min" if err_min else "req/min, no 5xx"), "bad": err_min > 1},
            {"label": "PVC storage", "value": f"{int(100 * pvc_used / pvc_cap) if pvc_cap else 0}%", "sub": f"{len(pvc_full)} over 85%" if pvc_full else f"{pvc_used / 2**30:.0f} of {pvc_cap / 2**30:.0f} GB", "bad": bool(pvc_full)},
            {"label": "Bitcoin node", "value": "synced" if node_sync >= 0.9999 else f"{100 * node_sync:.1f}%", "sub": f"{int(prom.scalar('bitcoin_peers'))} peers", "bad": node_sync < 0.999},
            {"label": "M5 inference", "value": f"{lemonade_models} model{'s' if lemonade_models != 1 else ''}", "sub": f"GPU {m5_gfx_temp:.0f}C" + (", throttling" if m5_thermal > 0 else ", capped" if m5_throttle > 0 else ", cool"), "bad": m5_thermal > 0},
        ]

        # ---- departures board rows ----
        def row(service, detail, status, note=""):
            return {"service": service, "detail": detail, "status": status, "note": note}

        rows = []
        rows.append(row("k3s cluster", f"{int(sum(pods.values()))} pods on {len(nodes)} nodes", "on time"))
        rows.append(row("Flux GitOps", f"{int(flux_ready)} of {int(flux_ready + flux_failed)} reconciled", "delayed" if flux_failed else "on time", ", ".join(flux_failed_names)))
        rows.append(row("Traefik ingress", f"{req_min:.0f} req/min", "delayed" if err_min > 1 else "on time", f"{err_min:.1f} 5xx/min" if err_min else "no errors"))
        rows.append(row("Monitoring", f"{total - len(down)} of {total} targets up", "delayed" if down else "on time", ", ".join(down)))
        rows.append(row("Storage", f"{int(100 * pvc_used / pvc_cap) if pvc_cap else 0}% used", "delayed" if pvc_full else "on time", f"{len(pvc_full)} volumes over 85%" if pvc_full else ""))
        rows.append(row("Bitcoin node", "synced" if node_sync >= 0.9999 else f"{100 * node_sync:.1f}% synced", "on time" if node_sync >= 0.999 else "delayed", f"{int(prom.scalar('bitcoin_peers'))} peers"))
        rows.append(row("M5 inference", f"{lemonade_models} model{'s' if lemonade_models != 1 else ''} loaded", "delayed" if m5_thermal > 0 else "on time", f"GPU {m5_gfx_temp:.0f}C" + (", thermal throttle" if m5_thermal > 0 else ", power cap" if m5_throttle > 0 else "")))
        for h in hosts:
            if not h["online"]:
                rows.append(row(h["name"], "no metrics", "cancelled", "host offline"))
                continue
            problems_here = []
            if h["mem_pct"] > 90:
                problems_here.append(f"memory {h['mem_pct']}%")
            if h["disk_pct"] > 85:
                problems_here.append(f"disk {h['disk_pct']}%")
            rows.append(row(h["name"], f"mem {h['mem_pct']}%, disk {h['disk_pct']}%", "delayed" if problems_here else "on time", ", ".join(problems_here)))
        delays = sum(1 for r in rows if r["status"] != "on time")

        alerts = []
        for name in down:
            alerts.append(f"{name} target down")
        for n in flux_failed_names:
            alerts.append(f"flux {n} failed")
        if err_min > 1:
            alerts.append(f"traefik {err_min:.0f} 5xx/min")
        pvc_full.sort(key=lambda kv: -kv[1])
        for n, v in pvc_full[:3]:
            alerts.append(f"{n} {v:.0f}% full")
        if len(pvc_full) > 3:
            alerts.append(f"{len(pvc_full) - 3} more volumes over 85%")
        for h in hosts:
            if not h["online"]:
                alerts.append(f"{h['name']} offline")
            else:
                if h["mem_pct"] > 90:
                    alerts.append(f"{h['name']} memory {h['mem_pct']}%")
                if h["disk_pct"] > 85:
                    alerts.append(f"{h['name']} disk {h['disk_pct']}%")
        if m5_thermal > 0:
            alerts.append("M5 thermal throttling")

        template_params = {
            "alerts": alerts,
            "cluster": cluster,
            "rows": rows,
            "delays": delays,
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "tiles": tiles,
            "nodes": nodes,
            "hosts": hosts,
            "pods_total": int(sum(pods.values())),
            "problems": len(down) + int(flux_failed) + (1 if pvc_full else 0),
            "ns_mem": [(n, v, int(100 * v / ns_max)) for n, v in ns_mem],
            "pvc_top": pvc_top,
            "plugin_settings": settings,
        }
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return self.render_image(dimensions, "homelab_status.html", "homelab_status.css", template_params)
