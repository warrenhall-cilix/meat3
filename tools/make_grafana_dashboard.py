#!/usr/bin/env python3
"""Generate packaging/grafana/meat3-dashboard.json (Grafana import, Prometheus data source).

    python3 tools/make_grafana_dashboard.py

One dashboard for any number of machines: pick hosts with the "instance" variable. Panels use
the sensor ids meat3 shows in its Overview, so they work for NVIDIA, AMD GPUs and AMD/Intel CPUs.
"""
import json
import os

DS = {"type": "prometheus", "uid": "${datasource}"}
SEL = 'instance=~"$instance"'
LEG = "{{instance}} {{sensor}}"
_id = [0]


def panel(title, expr, unit, x, y, w=12, h=8, legend=LEG, kind="timeseries", extra=None):
    _id[0] += 1
    p = {"id": _id[0], "type": kind, "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": legend, "datasource": DS}
                     for i, e in enumerate(expr if isinstance(expr, list) else [expr])],
         "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 8}}, "overrides": []},
         "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max", "mean"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}}}
    if extra:
        p.update(extra)
    return p


def row(title, y):
    _id[0] += 1
    return {"id": _id[0], "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


GPU_TEMP = r'nv\d+\.t\.(gpu|hotspot|mem)|amdgpu\d+\.gm\.temperature_(edge|hotspot|mem)'
GPU_POWER = r'nv\d+\.pwr\.board_real|amdgpu\d+\.gm\.(average|current)_socket_power|amdgpu\d+\.vrm\.real_power'
GPU_CLK = r'nv\d+\.clk\.(gpc|mem_eff)|amdgpu\d+\.gm\.(average|current)_(gfxclk|uclk)_frequency'
GPU_LOAD = r'nv\d+\.(util|load)\.gpu|amdgpu\d+\.gm\.average_gfx_activity'
CPU_TEMP = r'cpu\d*\.(k10temp\.temp1|temp\.tctl|cores\.temp_max)|cpu\d+\.coretemp\.temp1'
CPU_POWER = r'cpu\d*\.(pmt\.ppt|rapl\.intel_rapl_0|pwr\.package)'
CPU_CLK = r'cpu\d*\.clk\.(eff_avg|eff_max)'
CPU_LOAD = r'cpu\d*\.util\.total'

y = 0
panels = [
    panel("Up", f"meat3_up{{{SEL}}}", "none", 0, y, 6, 4, "{{instance}}", "stat",
          {"options": {"colorMode": "background", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"]}},
           "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {"0": {"text": "daemon down", "color": "red"},
                                                                                  "1": {"text": "up", "color": "green"}}}]},
                           "overrides": []}}),
    panel("Hottest sensor per machine", f'max by (instance) (meat3_temperature_celsius{{{SEL}}})', "celsius",
          6, y, 6, 4, "{{instance}}", "stat",
          {"options": {"colorMode": "value", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"]}},
           "fieldConfig": {"defaults": {"unit": "celsius", "thresholds": {"mode": "absolute", "steps": [
               {"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 95}]}},
                           "overrides": []}}),
    panel("Total GPU + CPU power", [f'sum by (instance) (meat3_power_watts{{{SEL},id=~`{GPU_POWER}`}})',
                                    f'sum by (instance) (meat3_power_watts{{{SEL},id=~`{CPU_POWER}`}})'],
          "watt", 12, y, 12, 4, "{{instance}}", "stat",
          {"options": {"colorMode": "value", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"]}}}),
]
y += 4
panels.append(row("GPU", y)); y += 1
panels += [panel("GPU temperatures", f'meat3_temperature_celsius{{{SEL},id=~`{GPU_TEMP}`}}', "celsius", 0, y),
           panel("GPU power", f'meat3_power_watts{{{SEL},id=~`{GPU_POWER}`}}', "watt", 12, y)]
y += 8
panels += [panel("GPU clocks", f'meat3_frequency_hertz{{{SEL},id=~`{GPU_CLK}`}}', "hertz", 0, y),
           panel("GPU load", f'meat3_percent{{{SEL},id=~`{GPU_LOAD}`}}', "percent", 12, y)]
y += 8
panels.append(row("CPU", y)); y += 1
panels += [panel("CPU temperature", f'meat3_temperature_celsius{{{SEL},id=~`{CPU_TEMP}`}}', "celsius", 0, y),
           panel("CPU power", f'meat3_power_watts{{{SEL},id=~`{CPU_POWER}`}}', "watt", 12, y)]
y += 8
panels += [panel("CPU clock (effective)", f'meat3_frequency_hertz{{{SEL},id=~`{CPU_CLK}`}}', "hertz", 0, y),
           panel("CPU usage", f'meat3_percent{{{SEL},id=~`{CPU_LOAD}`}}', "percent", 12, y)]
y += 8
panels.append(row("Motherboard", y)); y += 1
panels += [panel("Board temperatures", f'meat3_temperature_celsius{{{SEL},id=~`board\\..*`}}', "celsius", 0, y),
           panel("Fans", f'meat3_fan_speed_rpm{{{SEL}}} > 0', "rpm", 12, y),
           ]
y += 8
panels += [panel("Supply rails", f'meat3_voltage_volts{{{SEL},id=~`board\\..*`,sensor=~`\\+(12|5|3\\.3)V`}}',
                 "volt", 0, y),
           panel("CPU / memory voltages", f'meat3_voltage_volts{{{SEL},id=~`board\\..*`,sensor=~"Vcore|.*SoC.*|.*VDDIO.*|.*Misc.*"}}',
                 "volt", 12, y)]
y += 8
panels.append(row("Storage", y)); y += 1
panels += [panel("Drive temperatures", f'meat3_temperature_celsius{{{SEL},id=~`storage\\..*`}}',
                 "celsius", 0, y, legend="{{instance}} {{group}} {{sensor}}"),
           panel("Drive throughput", f'meat3_throughput_bytes_per_second{{{SEL},id=~`storage\\..*`}}',
                 "Bps", 12, y, legend="{{instance}} {{group}} {{sensor}}")]
y += 8
panels += [panel("NVMe life remaining", f'meat3_percent{{{SEL},id=~`storage\\..*\\.smart\\.left`}}', "percent",
                 0, y, 8, 6, "{{instance}} {{group}}", "bargauge",
                 {"fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "thresholds": {
                     "mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 10},
                                                   {"color": "green", "value": 30}]}}, "overrides": []},
                  "options": {"displayMode": "lcd", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]}}}),
           panel("Total written", f'meat3_size_bytes{{{SEL},id=~`storage\\..*\\.smart\\.written`}}', "decbytes",
                 8, y, 8, 6, "{{instance}} {{group}}", "bargauge",
                 {"options": {"displayMode": "basic", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]}}}),
           panel("Media errors / unsafe shutdowns",
                 [f'meat3_value{{{SEL},id=~`storage\\..*\\.smart\\.(media_err|unsafe|a5|a197)`}}'],
                 "none", 16, y, 8, 6, "{{instance}} {{group}} {{sensor}}", "table",
                 {"options": {"showHeader": True}, "transformations": [{"id": "reduce", "options": {"reducers": ["lastNotNull"]}}]})]

dash = {
    "title": "meat³ hardware",
    "uid": "meat3-hardware",
    "tags": ["meat3", "hardware"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Data source"},
        {"name": "instance", "type": "query", "label": "Machine", "datasource": DS,
         "query": {"query": "label_values(meat3_up, instance)", "refId": "q"},
         "definition": "label_values(meat3_up, instance)", "multi": True, "includeAll": True,
         "current": {"text": "All", "value": "$__all"}, "refresh": 2},
    ]},
    "panels": panels,
    "description": "meat³ (github.com/warrenhall-cilix/meat3) sensors via meat3-web /metrics",
}

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging", "grafana", "meat3-dashboard.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(dash, f, indent=1)
    f.write("\n")
print(f"wrote {out} ({len(panels)} panels)")
