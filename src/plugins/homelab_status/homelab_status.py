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
# Optional grouping of those instances into captioned columns, e.g. {"Proxmox": ["pve", "m70q-1"], "Storage": ["nas"]}.
HOST_GROUPS = local_setting("host_groups", {})
HOST_JOB = local_setting("host_metrics_job", "host-metrics")
TARGET_NAMES = {
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
        template_params["defaults"] = {"prometheusUrl": DEFAULT_PROMETHEUS_URL, "page": "hosts"}
        template_params["style_settings"] = True
        return template_params

    @staticmethod
    def fmt_uptime(seconds):
        if seconds is None:
            return ""
        days = int(seconds // 86400)
        if days:
            return f"up {days}d"
        return f"up {int(seconds // 3600)}h"

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
        # Hypervisors pin hugepages to their VMs, which makes MemAvailable look exhausted; measure the memory left to the host itself.
        mem_total = {m.get("instance"): v for m, v in prom.rows('node_memory_MemTotal_bytes{job="' + HOST_JOB + '"}')}
        mem_avail = {m.get("instance"): v for m, v in prom.rows('node_memory_MemAvailable_bytes{job="' + HOST_JOB + '"}')}
        huge_bytes = {m.get("instance"): v for m, v in prom.rows('node_memory_HugePages_Total{job="' + HOST_JOB + '"} * node_memory_Hugepagesize_bytes')}
        mem_pct = {}
        for inst, inst_total in mem_total.items():
            host_total = inst_total - huge_bytes.get(inst, 0)  # a NAS reports no hugepage series at all
            if host_total > 0 and inst in mem_avail:
                mem_pct[inst] = min(100.0, 100 * (1 - mem_avail[inst] / host_total))
        disk_pct = {m.get("instance"): v for m, v in prom.rows('100 * (1 - node_filesystem_avail_bytes{job="' + HOST_JOB + '",mountpoint="/"} / node_filesystem_size_bytes{job="' + HOST_JOB + '",mountpoint="/"})')}
        temp = {m.get("instance"): v for m, v in prom.rows('max by (instance)(node_hwmon_temp_celsius{job="' + HOST_JOB + '"})')}
        uptime = {m.get("instance"): v for m, v in prom.rows('node_time_seconds{job="' + HOST_JOB + '"} - node_boot_time_seconds')}
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
                "huge_gb": huge_bytes.get(inst, 0) / 2**30,
                "disk_pct": int(disk_pct.get(inst, 0)),
                "temp": temp.get(inst),
                "uptime": self.fmt_uptime(uptime.get(inst)),
            })

        # ---- services ----
        # Flux dropped gotk_reconcile_condition in 2.4; fall back to reconcile activity when it is absent.
        flux_ready = prom.scalar('sum(gotk_reconcile_condition{type="Ready",status="True"})')
        flux_failed = prom.scalar('sum(gotk_reconcile_condition{type="Ready",status="False"})')
        flux_failed_names = [m.get("name", "?") for m, v in prom.rows('gotk_reconcile_condition{type="Ready",status="False"} == 1')]
        flux_reconciles_h = prom.scalar("sum(increase(gotk_reconcile_duration_seconds_count[1h]))")
        flux_controllers = int(prom.scalar('count(up{job="monitoring/flux-system"} == 1)'))
        flux_controllers_total = int(prom.scalar('count(up{job="monitoring/flux-system"})'))
        req_min = prom.scalar("sum(rate(traefik_service_requests_total[15m]))*60")
        err_min = prom.scalar('sum(rate(traefik_service_requests_total{code=~"5.."}[15m]))*60')
        pvc_used = prom.scalar("sum(kubelet_volume_stats_used_bytes)")
        pvc_cap = prom.scalar("sum(kubelet_volume_stats_capacity_bytes)")
        pvc_full = [
            (m.get("persistentvolumeclaim", "?"), v)
            for m, v in prom.rows("100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes > 85")
        ]
        # Longhorn replicates every volume to each node, so one node's pool is the cluster's real disk footprint.
        lh_used = prom.scalar("max(sum by (node)(longhorn_node_storage_usage_bytes))")
        lh_cap = prom.scalar("max(sum by (node)(longhorn_node_storage_capacity_bytes))")
        lh_nodes = int(prom.scalar("count(sum by (node)(longhorn_node_storage_capacity_bytes))"))
        m5_thermal = prom.scalar('sum(increase(m5_throttle_residency_total{reason=~"thm_.*"}[24h]))')
        sites_up = int(prom.scalar('count(up{job="public-sites"} == 1)'))
        sites_total = int(prom.scalar('count(up{job="public-sites"})'))
        sites_down = [m.get("instance", "?").split("//")[-1].split("/")[0] for m, v in prom.rows('up{job="public-sites"} == 0')]
        dns_up = int(prom.scalar('count(up{job="' + HOST_JOB + '",instance=~"dns-.*"} == 1)'))
        dns_total = int(prom.scalar('count(up{job="' + HOST_JOB + '",instance=~"dns-.*"})'))
        bao_up = prom.scalar('max(up{job="openbao"})')
        node_sync = prom.scalar("bitcoin_verification_progress")
        node_peers = int(prom.scalar("bitcoin_peers"))

        cluster = {
            "pods": int(sum(pods.values())),
            "nodes_ready": int(prom.scalar('count(up{job="kubelet"} == 1)')),
            "nodes_total": int(prom.scalar('count(up{job="kubelet"})')),
            "cores_used": prom.scalar('sum(rate(container_cpu_usage_seconds_total{id="/"}[5m]))'),
            "cores_total": int(prom.scalar("sum(machine_cpu_cores)")),
            "containers": int(sum(containers.values())),
            "mem_used_gb": prom.scalar('sum(container_memory_working_set_bytes{id="/"})') / 2**30,
            "mem_alloc_gb": prom.scalar('sum(kube_node_status_allocatable{resource="memory"})') / 2**30,
            "restarts_24h": int(prom.scalar("sum(increase(kube_pod_container_status_restarts_total[24h]))")),
            "lh_used_gb": lh_used / 2**30,
            "lh_cap_gb": lh_cap / 2**30,
            "lh_nodes": lh_nodes,
        }

        if flux_ready + flux_failed > 0:
            flux_tile = {"label": "Flux", "value": f"{int(flux_ready)}/{int(flux_ready + flux_failed)}", "sub": ", ".join(flux_failed_names) if flux_failed_names else "reconciled", "bad": flux_failed > 0}
        else:
            flux_tile = {"label": "Flux", "value": f"{flux_controllers}/{flux_controllers_total}", "sub": f"{int(flux_reconciles_h)} runs/h", "bad": flux_controllers < flux_controllers_total}
        storage_tile = {"label": "Volumes", "value": f"{int(100 * pvc_used / pvc_cap) if pvc_cap else 0}%", "sub": f"{len(pvc_full)} vol{'s' if len(pvc_full) != 1 else ''} over 85%" if pvc_full else f"{pvc_used / 2**30:.0f} of {pvc_cap / 2**30:.0f} GB used", "bad": bool(pvc_full)}
        tiles = [
            {"label": "Monitoring", "value": f"{total - len(down)}/{total}", "sub": ", ".join(down) if down else "all targets up", "bad": bool(down)},
            flux_tile,
            {"label": "Traefik", "value": f"{req_min:.0f}", "sub": (f"req/min, {err_min:.1f} 5xx/min" if err_min else "req/min, no 5xx"), "bad": err_min > 1},
            storage_tile,
            {"label": "Public sites", "value": f"{sites_up}/{sites_total}", "sub": ", ".join(sites_down) if sites_down else "all responding", "bad": sites_up < sites_total},
            {"label": "DNS", "value": f"{dns_up}/{dns_total}", "sub": "Technitium pair" if dns_up == dns_total else "resolver down", "bad": dns_up < dns_total},
            {"label": "OpenBao", "value": "up" if bao_up else "down", "sub": "secrets vault", "bad": not bao_up},
            {"label": "Bitcoin node", "value": "synced" if node_sync >= 0.9999 else f"{100 * node_sync:.1f}%", "sub": f"{node_peers} peers", "bad": node_sync < 0.999},
        ]

        # ---- host groups for the cards row ----
        by_inst = {inst: h for inst, h in zip(host_keys, hosts)}
        groups = []
        placed = set()
        for gname, members in HOST_GROUPS.items():
            members = [i for i in members if i in by_inst]
            if members:
                groups.append({"name": gname, "hosts": [by_inst[i] for i in members]})
                placed.update(members)
        rest = [by_inst[i] for i in host_keys if i not in placed]
        if rest:
            groups.append({"name": "" if groups else "Hosts", "hosts": rest})

        alerts = []
        for name in down:
            alerts.append(f"{name} target down")
        for n in flux_failed_names:
            alerts.append(f"flux {n} failed")
        if flux_controllers < flux_controllers_total:
            alerts.append(f"flux {flux_controllers_total - flux_controllers} controller{'s' if flux_controllers_total - flux_controllers != 1 else ''} down")
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
        for site in sites_down:
            alerts.append(f"{site} down")
        if dns_up < dns_total:
            alerts.append(f"dns {dns_total - dns_up} of {dns_total} down")
        if not bao_up:
            alerts.append("openbao down")
        if node_sync < 0.999:
            alerts.append(f"bitcoin node {100 * node_sync:.1f}% synced")

        services = [
            {"name": t["label"], "value": t["value"], "note": t["sub"], "bad": t["bad"]} for t in tiles
        ]
        page = (settings.get("page") or "hosts").lower()
        template_params = {
            "page": page,
            "page_no": 1 if page == "hosts" else 2,
            "services": services,
            "alerts": alerts,
            "cluster": cluster,
            "updated": now.strftime(time_format).lstrip("0"),
            "date": now.strftime("%A %-d %B"),
            "tiles": tiles,
            "nodes": nodes,
            "hosts": hosts,
            "groups": groups,
            "plugin_settings": settings,
        }
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return self.render_image(dimensions, "homelab_status.html", "homelab_status.css", template_params)
