#!/usr/bin/env python3
"""网卡实时流量监控 - 集中式多设备监控 - 纯Python标准库零依赖

部署方式:
  1. 编辑同目录下 devices.json，配置所有需要监控的设备
  2. python3 monitor.py
  3. 浏览器访问 http://本机IP:18888/
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote

PORT = 18888
SYSFS_PATH = "/sys/class/net"
RETENTION_DAYS = 30
DB_SAMPLE_INTERVAL = 10  # 每10秒存一次DB，减少磁盘占用
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "traffic.db")
DEVICES_PATH = os.path.join(SCRIPT_DIR, "devices.json")

device_stats = {}  # key: "设备名:接口名" -> {rx, tx, ts, status}
devices_config = []
lock = threading.Lock()
active_collectors = {}  # key: "设备名:接口名" -> Thread
selected_ifaces = {}   # key: 设备名 -> 当前选的接口名


def get_local_ips():
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


def read_local_bytes(iface):
    try:
        with open(f"{SYSFS_PATH}/{iface}/statistics/rx_bytes") as f:
            rx = int(f.read().strip())
        with open(f"{SYSFS_PATH}/{iface}/statistics/tx_bytes") as f:
            tx = int(f.read().strip())
        return rx, tx
    except Exception:
        return None, None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS traffic (device TEXT, ts INTEGER, rx REAL, tx REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device_ts ON traffic(device, ts)")
    conn.commit()
    conn.close()


def insert_point(device, iface, ts, rx, tx):
    key = f"{device}:{iface}"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("INSERT INTO traffic (device, ts, rx, tx) VALUES (?, ?, ?, ?)",
                     (key, ts, rx, tx))
        conn.commit()
        conn.close()
    except Exception:
        pass


def query_history(device, iface, start_ts, end_ts):
    key = f"{device}:{iface}"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT ts, rx, tx FROM traffic WHERE device=? AND ts>=? AND ts<=? ORDER BY ts",
            (key, start_ts, end_ts))
        rows = [{"t": r[0], "rx": r[1], "tx": r[2]} for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def cleanup_db():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("DELETE FROM traffic WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def load_devices():
    global devices_config
    if not os.path.exists(DEVICES_PATH):
        devices_config = [{"name": "本机", "ip": "127.0.0.1", "iface": "ens160", "user": "root"}]
        with open(DEVICES_PATH, "w") as f:
            json.dump(devices_config, f, ensure_ascii=False, indent=2)
        print(f"已生成默认配置: {DEVICES_PATH}，请编辑后重启")
    else:
        with open(DEVICES_PATH) as f:
            devices_config = json.load(f)
    return devices_config


def local_collector(name, iface):
    prev_rx, prev_tx = read_local_bytes(iface)
    key = f"{name}:{iface}"
    if prev_rx is None:
        with lock:
            device_stats[key] = {"rx": 0, "tx": 0, "ts": int(time.time()), "status": "error"}
        return
    last_cleanup = 0
    last_db_ts = 0
    while True:
        time.sleep(1)
        rx, tx = read_local_bytes(iface)
        if rx is None:
            continue
        rx_rate = max(0, round((rx - prev_rx) * 8 / 1_000_000, 2))
        tx_rate = max(0, round((tx - prev_tx) * 8 / 1_000_000, 2))
        prev_rx, prev_tx = rx, tx
        ts = int(time.time())
        with lock:
            device_stats[key] = {"rx": rx_rate, "tx": tx_rate, "ts": ts, "status": "online"}
        if ts - last_db_ts >= DB_SAMPLE_INTERVAL:
            insert_point(name, iface, ts, rx_rate, tx_rate)
            last_db_ts = ts
        if ts - last_cleanup > 3600:
            cleanup_db()
            last_cleanup = ts


def ssh_collector(name, ip, iface, user, password=None):
    key = f"{name}:{iface}"
    while True:
        try:
            if password:
                cmd = ["sshpass", "-p", password, "ssh",
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "ConnectTimeout=5",
                       "-o", "ServerAliveInterval=5",
                       "-o", "ServerAliveCountMax=3",
                       f"{user}@{ip}",
                       f"while true; do cat /sys/class/net/{iface}/statistics/rx_bytes /sys/class/net/{iface}/statistics/tx_bytes; sleep 1; done"]
            else:
                cmd = ["ssh",
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "ConnectTimeout=5",
                       "-o", "ServerAliveInterval=5",
                       "-o", "ServerAliveCountMax=3",
                       f"{user}@{ip}",
                       f"while true; do cat /sys/class/net/{iface}/statistics/rx_bytes /sys/class/net/{iface}/statistics/tx_bytes; sleep 1; done"]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            prev_rx, prev_tx = None, None
            last_db_ts = 0
            with lock:
                device_stats[key] = {"rx": 0, "tx": 0, "ts": int(time.time()), "status": "connecting"}

            while True:
                line1 = proc.stdout.readline()
                if not line1:
                    break
                line2 = proc.stdout.readline()
                if not line2:
                    break
                try:
                    rx = int(line1.strip())
                    tx = int(line2.strip())
                except ValueError:
                    continue
                if prev_rx is not None:
                    rx_rate = max(0, round((rx - prev_rx) * 8 / 1_000_000, 2))
                    tx_rate = max(0, round((tx - prev_tx) * 8 / 1_000_000, 2))
                    ts = int(time.time())
                    with lock:
                        device_stats[key] = {"rx": rx_rate, "tx": tx_rate, "ts": ts, "status": "online"}
                    if ts - last_db_ts >= DB_SAMPLE_INTERVAL:
                        insert_point(name, iface, ts, rx_rate, tx_rate)
                        last_db_ts = ts
                prev_rx, prev_tx = rx, tx

            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass
        with lock:
            device_stats[key] = {"rx": 0, "tx": 0, "ts": int(time.time()), "status": "offline"}
        time.sleep(10)


def ensure_collector(name, ip, iface, user, password=None):
    key = f"{name}:{iface}"
    if key in active_collectors and active_collectors[key].is_alive():
        return
    local_ips = get_local_ips()
    if ip in local_ips:
        t = threading.Thread(target=local_collector, args=(name, iface), daemon=True)
    else:
        t = threading.Thread(target=ssh_collector, args=(name, ip, iface, user, password), daemon=True)
    t.start()
    active_collectors[key] = t


def start_collectors():
    for dev in devices_config:
        name = dev.get("name", dev.get("ip", "unknown"))
        ip = dev.get("ip", "127.0.0.1")
        iface = dev.get("iface", "ens160")
        user = dev.get("user", "root")
        password = dev.get("password")
        selected_ifaces[name] = iface
        ensure_collector(name, ip, iface, user, password)


def get_device_interfaces(dev):
    ip = dev.get("ip", "127.0.0.1")
    local_ips = get_local_ips()
    if ip in local_ips:
        try:
            return sorted(d for d in os.listdir(SYSFS_PATH)
                          if os.path.isdir(f"{SYSFS_PATH}/{d}")
                          and os.path.exists(f"{SYSFS_PATH}/{d}/statistics/rx_bytes"))
        except OSError:
            return []
    else:
        user = dev.get("user", "root")
        password = dev.get("password")
        try:
            if password:
                cmd = ["sshpass", "-p", password, "ssh",
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "ConnectTimeout=5",
                       f"{user}@{ip}",
                       f"ls {SYSFS_PATH}"]
            else:
                cmd = ["ssh",
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "ConnectTimeout=5",
                       f"{user}@{ip}",
                       f"ls {SYSFS_PATH}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                return [i.strip() for i in result.stdout.strip().split('\n') if i.strip()]
        except Exception:
            pass
        return []


def fmt_mbps(v):
    if v < 0.01:
        return "0 Mbps"
    if v < 1:
        return f"{v:.3f} Mbps"
    if v < 1000:
        return f"{v:.2f} Mbps"
    return f"{v/1000:.2f} Gbps"


def fmt_mbps_html(v):
    if v < 0.01:
        return '<span style="color:#666">0 Mbps</span>'
    if v < 1:
        return f'{v:.3f} Mbps'
    if v < 1000:
        return f'{v:.2f} Mbps'
    return f'{v/1000:.2f} Gbps'


def build_device_list_html(current_name=""):
    html = ""
    for idx, dev in enumerate(devices_config):
        name = dev.get("name", dev.get("ip", "unknown"))
        ip = dev.get("ip", "")
        iface = selected_ifaces.get(name, dev.get("iface", "ens160"))
        key = f"{name}:{iface}"
        with lock:
            stat = device_stats.get(key, {"rx": 0, "tx": 0, "status": "offline"})
        status = stat.get("status", "offline")
        rx = stat.get("rx", 0)
        tx = stat.get("tx", 0)
        active = " active" if name == current_name else ""
        html += f'''<div class="device-item{active}" data-device="{name}" id="dev-{idx}" onclick="selectDevice({idx})">
  <div class="device-name"><span class="status-dot {status}"></span>{name}</div>
  <div class="device-ip">{ip} / {iface}</div>
  <div class="device-rates" id="rates-{idx}"><span class="rx">&darr; {fmt_mbps(rx)}</span> <span class="tx">&uarr; {fmt_mbps(tx)}</span></div>
</div>'''
    return html


def build_svg_chart(data_points, mode="both", title="", width=900, height=250):
    if not data_points:
        return f'<div class="no-chart">暂无数据{(": " + title) if title else ""}</div>'

    margin_l, margin_r, margin_t, margin_b = 60, 20, 30, 40
    cw = width - margin_l - margin_r
    ch = height - margin_t - margin_b

    all_rx = [d["rx"] for d in data_points]
    all_tx = [d["tx"] for d in data_points]
    show_rx = mode in ("both", "rx")
    show_tx = mode in ("both", "tx")
    vals = []
    if show_rx: vals.extend(all_rx)
    if show_tx: vals.extend(all_tx)
    max_val = max(max(vals), 0.01) * 1.1
    n = len(data_points)

    def to_x(i): return margin_l + (i / max(n - 1, 1)) * cw
    def to_y(v): return margin_t + ch - (v / max_val) * ch

    y_ticks = ""
    for i in range(5):
        v = max_val * i / 4
        y = to_y(v)
        y_ticks += f'<line x1="{margin_l}" y1="{y}" x2="{width-margin_r}" y2="{y}" stroke="#2a2a4a" stroke-width="1"/>'
        y_ticks += f'<text x="{margin_l-5}" y="{y+4}" text-anchor="end" fill="#777" font-size="10">{fmt_mbps(v)}</text>'

    x_ticks = ""
    step = max(1, n // 8)
    for i in range(0, n, step):
        x = to_x(i)
        ts = data_points[i]["t"]
        x_ticks += f'<text x="{x}" y="{height-5}" text-anchor="middle" fill="#555" font-size="9">{time.strftime("%H:%M:%S", time.localtime(ts))}</text>'

    paths = ""
    lx = margin_l
    if show_rx:
        rp = " ".join(f"M{to_x(0)},{to_y(all_rx[0])}" if i == 0 else f"L{to_x(i)},{to_y(all_rx[i])}" for i in range(n))
        rf = rp.replace("M", "M", 1) + f" L{to_x(n-1)},{margin_t+ch} L{to_x(0)},{margin_t+ch} Z"
        paths += f'<path d="{rf}" fill="#4fc3f722"/><path d="{rp}" fill="none" stroke="#4fc3f7" stroke-width="1.5"/>'
        paths += f'<text x="{lx}" y="{margin_t-8}" fill="#4fc3f7" font-size="11">↓ 入站</text>'
        lx += 80
    if show_tx:
        tp = " ".join(f"M{to_x(0)},{to_y(all_tx[0])}" if i == 0 else f"L{to_x(i)},{to_y(all_tx[i])}" for i in range(n))
        tf = tp.replace("M", "M", 1) + f" L{to_x(n-1)},{margin_t+ch} L{to_x(0)},{margin_t+ch} Z"
        paths += f'<path d="{tf}" fill="#ff8a6522"/><path d="{tp}" fill="none" stroke="#ff8a65" stroke-width="1.5"/>'
        paths += f'<text x="{lx}" y="{margin_t-8}" fill="#ff8a65" font-size="11">↑ 出站</text>'

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">
  <rect width="{width}" height="{height}" fill="#1a1a2e"/>{y_ticks}{x_ticks}{paths}
</svg>'''


def build_report_html(range_sec):
    """生成报表HTML页面：每台设备一个卡片，含详细统计+折线图"""
    end_ts = int(time.time())
    start_ts = end_ts - range_sec
    time_range_label = {600: "10分钟", 3600: "1小时", 21600: "6小时", 86400: "24小时", 604800: "7天"}.get(range_sec, f"{range_sec}秒")
    start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts))
    end_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ts))

    # Summary totals
    total_rx_avg = 0
    total_tx_avg = 0
    total_rx_max = 0
    total_tx_max = 0

    # 一览表数据
    overview_rows = []

    cards = ""
    for dev in devices_config:
        name = dev.get("name", dev.get("ip", "unknown"))
        ip = dev.get("ip", "")
        iface = selected_ifaces.get(name, dev.get("iface", "ens160"))
        key = f"{name}:{iface}"

        with lock:
            stat = device_stats.get(key, {"rx": 0, "tx": 0, "status": "offline", "ts": 0})
        status = stat.get("status", "offline")

        data = query_history(name, iface, start_ts, end_ts)
        # Downsample for SVG
        svg_data = data[::max(1, len(data)//300)] if len(data) > 300 else data
        svg = build_svg_chart(svg_data, mode="both", title=f"{name} ({iface})", width=800, height=220)

        # Stats
        if data:
            rx_vals = [d["rx"] for d in data]
            tx_vals = [d["tx"] for d in data]
            rx_avg = sum(rx_vals) / len(rx_vals)
            tx_avg = sum(tx_vals) / len(tx_vals)
            rx_max = max(rx_vals)
            tx_max = max(tx_vals)
            rx_max_ts = data[rx_vals.index(rx_max)]["t"]
            tx_max_ts = data[tx_vals.index(tx_max)]["t"]
            rx_total_gb = sum(rx_vals) * 1 / 8000  # Mbps * seconds / 8 / 1000 = GB
            tx_total_gb = sum(tx_vals) * 1 / 8000
        else:
            rx_avg = tx_avg = rx_max = tx_max = rx_total_gb = tx_total_gb = 0
            rx_max_ts = tx_max_ts = 0

        total_rx_avg += rx_avg
        total_tx_avg += tx_avg
        total_rx_max = max(total_rx_max, rx_max)
        total_tx_max = max(total_tx_max, tx_max)

        status_color = "#4caf50" if status == "online" else "#f44336" if status == "offline" else "#ff9800"
        status_text = {"online": "在线", "offline": "离线", "connecting": "连接中", "error": "错误"}.get(status, status)

        rx_max_time = time.strftime("%m-%d %H:%M:%S", time.localtime(rx_max_ts)) if rx_max_ts else "-"
        tx_max_time = time.strftime("%m-%d %H:%M:%S", time.localtime(tx_max_ts)) if tx_max_ts else "-"

        overview_rows.append({
            "name": name, "ip": ip, "iface": iface, "status": status,
            "rx_avg": rx_avg, "tx_avg": tx_avg,
            "rx_max": rx_max, "tx_max": tx_max,
            "rx_max_time": rx_max_time, "tx_max_time": tx_max_time,
            "rx_total_gb": rx_total_gb, "tx_total_gb": tx_total_gb,
        })

        cards += f'''
<div class="card">
  <div class="card-header">
    <h3>{name}</h3>
    <span class="status-badge" style="background:{status_color}">{status_text}</span>
  </div>
  <div class="card-meta">{ip} / {iface}</div>
  <div class="stats-row">
    <div class="stat-box"><div class="stat-label">入站均值</div><div class="stat-val" style="color:#4fc3f7">{fmt_mbps(rx_avg)}</div></div>
    <div class="stat-box"><div class="stat-label">入站峰值</div><div class="stat-val" style="color:#4fc3f7">{fmt_mbps(rx_max)}</div><div class="stat-time">{rx_max_time}</div></div>
    <div class="stat-box"><div class="stat-label">出站均值</div><div class="stat-val" style="color:#ff8a65">{fmt_mbps(tx_avg)}</div></div>
    <div class="stat-box"><div class="stat-label">出站峰值</div><div class="stat-val" style="color:#ff8a65">{fmt_mbps(tx_max)}</div><div class="stat-time">{tx_max_time}</div></div>
    <div class="stat-box"><div class="stat-label">入站总量</div><div class="stat-val" style="color:#4fc3f7">{rx_total_gb:.2f} GB</div></div>
    <div class="stat-box"><div class="stat-label">出站总量</div><div class="stat-val" style="color:#ff8a65">{tx_total_gb:.2f} GB</div></div>
  </div>
  <div class="card-chart">{svg}</div>
</div>'''

    # 一览表
    overview_rows_html = ""
    for r in overview_rows:
        sc = "#4caf50" if r["status"] == "online" else "#f44336" if r["status"] == "offline" else "#ff9800"
        st = {"online": "在线", "offline": "离线", "connecting": "连接中", "error": "错误"}.get(r["status"], r["status"])
        overview_rows_html += f'''<tr>
  <td><span class="dot" style="background:{sc}"></span>{r['name']}</td>
  <td>{r['ip']}<br><span class="sub">{r['iface']}</span></td>
  <td class="rx">{fmt_mbps(r['rx_avg'])}</td>
  <td class="rx">{fmt_mbps(r['rx_max'])}<br><span class="sub">{r['rx_max_time']}</span></td>
  <td class="tx">{fmt_mbps(r['tx_avg'])}</td>
  <td class="tx">{fmt_mbps(r['tx_max'])}<br><span class="sub">{r['tx_max_time']}</span></td>
  <td class="rx">{r['rx_total_gb']:.2f} GB</td>
  <td class="tx">{r['tx_total_gb']:.2f} GB</td>
</tr>'''

    # Summary table
    summary = f'''
<div class="summary">
  <h2>汇总</h2>
  <table>
    <tr><th>指标</th><th style="color:#4fc3f7">入站</th><th style="color:#ff8a65">出站</th></tr>
    <tr><td>所有设备均值合计</td><td>{fmt_mbps(total_rx_avg)}</td><td>{fmt_mbps(total_tx_avg)}</td></tr>
    <tr><td>所有设备峰值</td><td>{fmt_mbps(total_rx_max)}</td><td>{fmt_mbps(total_tx_max)}</td></tr>
  </table>
</div>

<div class="overview">
  <h2>设备流量一览</h2>
  <div class="table-wrap">
  <table>
    <thead>
      <tr><th>设备</th><th>IP / 接口</th><th style="color:#4fc3f7">入站均值</th><th style="color:#4fc3f7">入站峰值(时间)</th><th style="color:#ff8a65">出站均值</th><th style="color:#ff8a65">出站峰值(时间)</th><th style="color:#4fc3f7">入站总量</th><th style="color:#ff8a65">出站总量</th></tr>
    </thead>
    <tbody>{overview_rows_html}</tbody>
  </table>
  </div>
</div>'''

    return f'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>流量报表 - {time_range_label}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:24px}}
h1{{font-size:20px;color:#fff;margin-bottom:4px}}
.meta{{color:#888;font-size:13px;margin-bottom:24px}}
.summary{{background:#16162a;border:1px solid #2a2a4a;border-radius:8px;padding:20px;margin-bottom:24px}}
.summary h2{{font-size:16px;color:#fff;margin-bottom:12px}}
.summary table{{width:100%;border-collapse:collapse}}
.summary th,.summary td{{padding:8px 12px;text-align:left;border-bottom:1px solid #2a2a4a;font-size:13px}}
.summary th{{color:#aaa}}
.card{{background:#16162a;border:1px solid #2a2a4a;border-radius:8px;padding:20px;margin-bottom:16px}}
.card-header{{display:flex;align-items:center;gap:10px;margin-bottom:4px}}
.card-header h3{{font-size:15px;color:#fff}}
.status-badge{{font-size:11px;padding:2px 8px;border-radius:10px;color:#fff}}
.card-meta{{color:#666;font-size:12px;margin-bottom:12px}}
.stats-row{{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
.stat-box{{background:#1a1a2e;border:1px solid #2a2a4a;border-radius:6px;padding:8px 14px;min-width:120px}}
.stat-box .stat-label{{font-size:11px;color:#888;margin-bottom:2px}}
.stat-box .stat-val{{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}}
.stat-time{{font-size:10px;color:#888;margin-top:2px}}
.card-chart{{height:220px}}
.overview{{background:#16162a;border:1px solid #2a2a4a;border-radius:8px;padding:20px;margin-bottom:24px}}
.overview h2{{font-size:16px;color:#fff;margin-bottom:12px}}
.table-wrap{{overflow-x:auto}}
.overview table{{width:100%;border-collapse:collapse;font-size:13px}}
.overview th{{padding:8px 10px;text-align:left;border-bottom:2px solid #2a2a4a;color:#aaa;font-size:12px;white-space:nowrap}}
.overview td{{padding:8px 10px;border-bottom:1px solid #2a2a4a;vertical-align:top}}
.overview td.rx{{color:#4fc3f7}}
.overview td.tx{{color:#ff8a65}}
.overview .sub{{font-size:10px;color:#888}}
.overview .dot{{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:4px;vertical-align:middle}}
.actions{{margin-top:20px;text-align:center}}
.btn{{background:#3a3a5a;color:#e0e0e0;border:none;padding:8px 20px;border-radius:4px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block;margin:0 8px}}
.btn:hover{{background:#4a4a6a}}
.btn-print{{background:#4fc3f7;color:#1a1a2e}}
.btn-word{{background:#4caf50;color:#fff}}
@media print{{body{{background:#fff;color:#333}}.card{{border-color:#ddd;background:#fff}}.stat-box{{border-color:#ddd;background:#f9f9f9}}.stat-val{{color:#333!important}}}}
</style>
</head>
<body>
<h1>流量监控报表</h1>
<div class="meta">时间范围: {time_range_label} ({start_str} ~ {end_str}) | 生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}</div>
{summary}
{cards}
<div class="actions">
  <a class="btn btn-word" href="/report/export?format=word&range={range_sec}">导出Word</a>
  <a class="btn btn-print" onclick="window.print()">打印 / 导出PDF</a>
  <a class="btn" href="/?device={devices_config[0].get("name","") if devices_config else ""}&range={range_sec}">返回监控</a>
</div>
</body>
</html>'''


def build_report_doc(range_sec):
    """生成Word兼容的HTML报表(.doc格式)，Word可直接打开"""
    end_ts = int(time.time())
    start_ts = end_ts - range_sec
    time_range_label = {600: "10分钟", 3600: "1小时", 21600: "6小时", 86400: "24小时", 604800: "7天"}.get(range_sec, f"{range_sec}秒")
    start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts))
    end_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ts))
    gen_str = time.strftime("%Y-%m-%d %H:%M:%S")

    total_rx_avg = 0
    total_tx_avg = 0
    total_rx_max = 0
    total_tx_max = 0
    overview_rows = []

    for dev in devices_config:
        name = dev.get("name", dev.get("ip", "unknown"))
        ip = dev.get("ip", "")
        iface = selected_ifaces.get(name, dev.get("iface", "ens160"))
        key = f"{name}:{iface}"

        with lock:
            stat = device_stats.get(key, {"rx": 0, "tx": 0, "status": "offline", "ts": 0})
        status = stat.get("status", "offline")
        status_text = {"online": "在线", "offline": "离线", "connecting": "连接中", "error": "错误"}.get(status, status)

        data = query_history(name, iface, start_ts, end_ts)
        if data:
            rx_vals = [d["rx"] for d in data]
            tx_vals = [d["tx"] for d in data]
            rx_avg = sum(rx_vals) / len(rx_vals)
            tx_avg = sum(tx_vals) / len(tx_vals)
            rx_max = max(rx_vals)
            tx_max = max(tx_vals)
            rx_max_ts = data[rx_vals.index(rx_max)]["t"]
            tx_max_ts = data[tx_vals.index(tx_max)]["t"]
            rx_total_gb = sum(rx_vals) / 8000
            tx_total_gb = sum(tx_vals) / 8000
        else:
            rx_avg = tx_avg = rx_max = tx_max = rx_total_gb = tx_total_gb = 0
            rx_max_ts = tx_max_ts = 0

        total_rx_avg += rx_avg
        total_tx_avg += tx_avg
        total_rx_max = max(total_rx_max, rx_max)
        total_tx_max = max(total_tx_max, tx_max)

        rx_max_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rx_max_ts)) if rx_max_ts else "-"
        tx_max_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx_max_ts)) if tx_max_ts else "-"

        overview_rows.append({
            "name": name, "ip": ip, "iface": iface, "status_text": status_text,
            "rx_avg": fmt_mbps(rx_avg), "tx_avg": fmt_mbps(tx_avg),
            "rx_max": fmt_mbps(rx_max), "tx_max": fmt_mbps(tx_max),
            "rx_max_time": rx_max_time, "tx_max_time": tx_max_time,
            "rx_total": f"{rx_total_gb:.2f} GB", "tx_total": f"{tx_total_gb:.2f} GB",
        })

    # 一览表行
    overview_rows_html = ""
    for r in overview_rows:
        overview_rows_html += f'''<tr>
  <td style="border:1px solid #ccc;padding:6px 8px">{r['name']} ({r['status_text']})</td>
  <td style="border:1px solid #ccc;padding:6px 8px">{r['ip']} / {r['iface']}</td>
  <td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{r['rx_avg']}</td>
  <td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{r['rx_max']}<br/>{r['rx_max_time']}</td>
  <td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{r['tx_avg']}</td>
  <td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{r['tx_max']}<br/>{r['tx_max_time']}</td>
  <td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{r['rx_total']}</td>
  <td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{r['tx_total']}</td>
</tr>'''

    # 汇总表
    summary_html = f'''<table style="border-collapse:collapse;width:100%">
  <tr><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">指标</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#1976D2;text-align:left">入站</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#E64A19;text-align:left">出站</th></tr>
  <tr><td style="border:1px solid #ccc;padding:6px 8px">所有设备均值合计</td><td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{fmt_mbps(total_rx_avg)}</td><td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{fmt_mbps(total_tx_avg)}</td></tr>
  <tr><td style="border:1px solid #ccc;padding:6px 8px">所有设备峰值</td><td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{fmt_mbps(total_rx_max)}</td><td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{fmt_mbps(total_tx_max)}</td></tr>
</table>'''

    # 一览表
    overview_html = f'''<table style="border-collapse:collapse;width:100%">
  <thead>
    <tr><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">设备</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">IP / 接口</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#1976D2;text-align:left">入站均值</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#1976D2;text-align:left">入站峰值(时间)</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#E64A19;text-align:left">出站均值</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#E64A19;text-align:left">出站峰值(时间)</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#1976D2;text-align:left">入站总量</th><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;color:#E64A19;text-align:left">出站总量</th></tr>
  </thead>
  <tbody>{overview_rows_html}</tbody>
</table>'''

    return f'''<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" xmlns="http://www.w3.org/TR/REC-html40">
<head>
<meta charset="utf-8">
<style>
body {{ font-family: "微软雅黑", SimSun, Arial, sans-serif; font-size: 12pt; margin: 30px; }}
h1 {{ font-size: 18pt; color: #333; margin-bottom: 4px; }}
h2 {{ font-size: 14pt; color: #333; margin-top: 20px; margin-bottom: 8px; border-bottom: 1px solid #999; padding-bottom: 4px; }}
.meta {{ color: #888; font-size: 10pt; margin-bottom: 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
</style>
</head>
<body>
<h1>流量监控报表</h1>
<div class="meta">时间范围: {time_range_label} ({start_str} ~ {end_str}) | 生成时间: {gen_str}</div>

<h2>汇总</h2>
{summary_html}

<h2>设备流量一览</h2>
{overview_html}

<h2>每设备详细统计</h2>
{_build_doc_device_details(overview_rows)}

</body>
</html>'''


def _build_doc_device_details(overview_rows):
    rows = ""
    for r in overview_rows:
        rows += f'''<h3 style="font-size:12pt;color:#333;margin-top:12px">{r['name']} ({r['status_text']})</h3>
<p style="color:#888;font-size:10pt;margin-bottom:6px">{r['ip']} / {r['iface']}</p>
<table style="border-collapse:collapse;width:80%">
  <tr><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">入站均值</th><td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{r['rx_avg']}</td>
      <th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">出站均值</th><td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{r['tx_avg']}</td></tr>
  <tr><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">入站峰值</th><td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{r['rx_max']}<br/>{r['rx_max_time']}</td>
      <th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">出站峰值</th><td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{r['tx_max']}<br/>{r['tx_max_time']}</td></tr>
  <tr><th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">入站总量</th><td style="border:1px solid #ccc;padding:6px 8px;color:#1976D2">{r['rx_total']}</td>
      <th style="border:1px solid #ccc;padding:6px 8px;background:#f0f0f0;text-align:left">出站总量</th><td style="border:1px solid #ccc;padding:6px 8px;color:#E64A19">{r['tx_total']}</td></tr>
</table>'''
    return rows


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>流量监控</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;height:100vh;overflow:hidden}
.sidebar{width:260px;min-width:260px;background:#16162a;border-right:1px solid #2a2a4a;display:flex;flex-direction:column}
.sidebar-title{padding:16px;font-size:15px;font-weight:600;color:#fff;border-bottom:1px solid #2a2a4a}
.device-list{flex:1;overflow-y:auto;padding:8px}
.device-item{padding:10px 12px;border-radius:6px;cursor:pointer;margin-bottom:4px;transition:background .15s}
.device-item:hover{background:#2a2a4a}
.device-item.active{background:#2a2a4a;border-left:3px solid #4fc3f7}
.device-name{font-size:13px;font-weight:500;color:#e0e0e0;margin-bottom:2px;display:flex;align-items:center;gap:6px}
.device-ip{font-size:11px;color:#666;margin-bottom:4px}
.device-rates{font-size:12px;font-variant-numeric:tabular-nums}
.device-rates .rx{color:#4fc3f7}
.device-rates .tx{color:#ff8a65}
.status-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.status-dot.online{background:#4caf50}
.status-dot.offline{background:#f44336}
.status-dot.connecting{background:#ff9800}
.status-dot.error{background:#f44336}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.topbar{padding:12px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #2a2a4a;gap:16px;flex-shrink:0}
.topbar h2{font-size:16px;font-weight:500;color:#fff}
.topbar-right{display:flex;align-items:center;gap:10px}
.topbar-right select{background:#2a2a4a;color:#e0e0e0;border:1px solid #3a3a5a;padding:5px 10px;border-radius:4px;font-size:12px}
.mode-btn{background:#2a2a4a;color:#888;border:1px solid #3a3a5a;padding:5px 10px;border-radius:4px;font-size:12px;cursor:pointer;text-decoration:none}
.mode-btn:hover{background:#3a3a5a}
.mode-btn.active-rx{color:#4fc3f7;border-color:#4fc3f7}
.mode-btn.active-tx{color:#ff8a65;border-color:#ff8a65}
.mode-btn.active-both{color:#fff;border-color:#888;background:#3a3a5a}
.report-btn{background:#4fc3f7;color:#1a1a2e;border:none;padding:5px 12px;border-radius:4px;font-size:12px;cursor:pointer;text-decoration:none;font-weight:600}
.report-btn:hover{background:#81d4fa}
.chart-wrap{flex:1;padding:12px 24px 16px;position:relative;min-height:0}
.no-chart{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#555;font-size:14px}
</style>
</head>
<body>
<div class="sidebar">
  <div class="sidebar-title">设备列表</div>
  <div class="device-list" id="device-list">__DEVICE_LIST__</div>
</div>
<div class="main">
  <div class="topbar">
    <h2 id="chart-title">__CHART_TITLE__</h2>
    <div class="topbar-right">
      <select id="iface-select" onchange="changeIface(this.value)">__IFACE_OPTIONS__</select>
      <a class="mode-btn __BTN_RX_CLASS__" href="__URL_RX__">入站</a>
      <a class="mode-btn __BTN_TX_CLASS__" href="__URL_TX__">出站</a>
      <a class="mode-btn __BTN_BOTH_CLASS__" href="__URL_BOTH__">全部</a>
      <select id="time-range" onchange="changeRange(this.value)">
        <option value="600"__SEL_600__>10分钟</option>
        <option value="3600"__SEL_3600__>1小时</option>
        <option value="21600"__SEL_21600__>6小时</option>
        <option value="86400"__SEL_86400__>24小时</option>
        <option value="604800"__SEL_604800__>7天</option>
      </select>
      <a class="report-btn" href="/report?range=__CURRENT_RANGE__">报表</a>
    </div>
  </div>
  <div class="chart-wrap">
    __SVG_CHART__
  </div>
</div>
<script>
var currentDevice = "__CURRENT_DEVICE__";
var currentIface = "__CURRENT_IFACE__";
var currentMode = "__CURRENT_MODE__";
var currentRange = "__CURRENT_RANGE__";
var selectedIfaces = __SELECTED_IFACES_DICT__;
function selectDevice(idx){
  var name = document.querySelectorAll('.device-item')[idx].dataset.device;
  var iface = selectedIfaces[name] || "ens160";
  window.location.href = "/?device=" + encodeURIComponent(name) + "&iface=" + encodeURIComponent(iface);
}
function changeRange(sec){
  window.location.href = "/?device=" + encodeURIComponent(currentDevice) + "&iface=" + encodeURIComponent(currentIface) + "&mode=" + currentMode + "&range=" + sec;
}
function changeIface(iface){
  window.location.href = "/?device=" + encodeURIComponent(currentDevice) + "&iface=" + encodeURIComponent(iface) + "&mode=" + currentMode + "&range=" + currentRange;
}
setTimeout(function(){ window.location.reload(); }, 5000);
</script>
</body>
</html>
"""


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == '/report':
            range_sec = int(params.get('range', ['600'])[0])
            html = build_report_html(range_sec)
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/report/export':
            range_sec = int(params.get('range', ['600'])[0])
            fmt = params.get('format', ['word'])[0]
            if fmt == 'word':
                filename = f"traffic_report_{time.strftime('%Y%m%d_%H%M')}.doc"
                filename_cn = f"流量监控报表_{time.strftime('%Y%m%d_%H%M')}.doc"
                content_type = 'application/msword'
                html = build_report_doc(range_sec)
                body = html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', f'{content_type}; charset=utf-8')
                self.send_header('Content-Disposition',
                    f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quote(filename_cn)}')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(400)
                self.end_headers()
            return

        if path in ('/', ''):
            device_name = params.get('device', [''])[0]
            iface = params.get('iface', [''])[0]
            mode = params.get('mode', ['both'])[0]
            range_sec = int(params.get('range', ['600'])[0])

            if not device_name and devices_config:
                device_name = devices_config[0].get("name", devices_config[0].get("ip", ""))

            dev_cfg = None
            for d in devices_config:
                if d.get("name", d.get("ip")) == device_name:
                    dev_cfg = d
                    break

            if iface:
                selected_ifaces[device_name] = iface
            elif device_name in selected_ifaces:
                iface = selected_ifaces[device_name]
            elif dev_cfg:
                iface = dev_cfg.get("iface", "ens160")
                selected_ifaces[device_name] = iface
            else:
                iface = "ens160"

            if dev_cfg and f"{device_name}:{iface}" not in active_collectors:
                ensure_collector(device_name, dev_cfg.get("ip", "127.0.0.1"),
                                iface, dev_cfg.get("user", "root"), dev_cfg.get("password"))

            end_ts = int(time.time())
            start_ts = end_ts - range_sec
            data = query_history(device_name, iface, start_ts, end_ts) if device_name else []
            if len(data) > 600:
                step = max(1, len(data) // 600)
                data = data[::step]

            svg = build_svg_chart(data, mode=mode, title=f"{device_name} ({iface})")
            device_list_html = build_device_list_html(current_name=device_name)

            iface_options = ""
            if dev_cfg:
                for i in get_device_interfaces(dev_cfg):
                    sel = " selected" if i == iface else ""
                    iface_options += f'<option value="{i}"{sel}>{i}</option>'
            else:
                iface_options = f'<option value="{iface}" selected>{iface}</option>'

            base_url = f"/?device={device_name}&iface={iface}&range={range_sec}"
            rx_class = "active-rx" if mode == "rx" else ""
            tx_class = "active-tx" if mode == "tx" else ""
            both_class = "active-both" if mode == "both" else ""

            sel = {}
            for v in ("600", "3600", "21600", "86400", "604800"):
                sel[v] = " selected" if str(range_sec) == v else ""

            page = HTML_PAGE
            page = page.replace("__DEVICE_LIST__", device_list_html)
            page = page.replace("__SVG_CHART__", svg)
            page = page.replace("__CHART_TITLE__", f"{device_name} ({iface})" if device_name else "选择设备")
            page = page.replace("__CURRENT_DEVICE__", device_name)
            page = page.replace("__CURRENT_IFACE__", iface)
            page = page.replace("__CURRENT_MODE__", mode)
            page = page.replace("__CURRENT_RANGE__", str(range_sec))
            page = page.replace("__SELECTED_IFACES_DICT__", json.dumps(selected_ifaces, ensure_ascii=False))
            page = page.replace("__IFACE_OPTIONS__", iface_options)
            page = page.replace("__BTN_RX_CLASS__", rx_class)
            page = page.replace("__BTN_TX_CLASS__", tx_class)
            page = page.replace("__BTN_BOTH_CLASS__", both_class)
            page = page.replace("__URL_RX__", base_url + "&mode=rx")
            page = page.replace("__URL_TX__", base_url + "&mode=tx")
            page = page.replace("__URL_BOTH__", base_url + "&mode=both")
            for v in ("600", "3600", "21600", "86400", "604800"):
                page = page.replace(f"__SEL_{v}__", sel[v])

            body = page.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == '/api/devices':
            result = []
            for dev in devices_config:
                name = dev.get("name", dev.get("ip", "unknown"))
                iface = selected_ifaces.get(name, dev.get("iface", "ens160"))
                key = f"{name}:{iface}"
                with lock:
                    stat = device_stats.get(key, {"rx": 0, "tx": 0, "status": "offline"})
                result.append({
                    "name": name, "ip": dev.get("ip", ""),
                    "iface": iface, "rx": stat.get("rx", 0), "tx": stat.get("tx", 0),
                    "status": stat.get("status", "offline")
                })
            self._send_json(result)

        elif path == '/api/history':
            device = params.get('device', [''])[0]
            iface = params.get('iface', ['ens160'])[0]
            end_ts = int(params.get('end', [str(int(time.time()))])[0])
            start_ts = int(params.get('start', [str(end_ts - 600)])[0])
            if not device:
                self._send_json({"error": "device required"}, 400)
                return
            data = query_history(device, iface, start_ts, end_ts)
            self._send_json(data)

        else:
            self.send_response(404)
            self.end_headers()


def main():
    load_devices()
    init_db()
    start_collectors()

    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f"流量监控已启动: http://0.0.0.0:{PORT}/")
    print(f"监控设备: {len(devices_config)} 台")
    for dev in devices_config:
        name = dev.get("name", dev.get("ip"))
        ip = dev.get("ip")
        iface = dev.get("iface", "ens160")
        print(f"  {name}: {ip} ({iface})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == '__main__':
    main()
