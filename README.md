# 网卡流量监控程序

集中式多设备网卡流量实时监控工具，纯Python标准库零依赖。

一台服务器部署，SSH连接所有设备采集数据，浏览器即可查看所有节点接口的实时流量和报表。

## 安装步骤

> 只需要在**一台服务器**上安装部署，其他被监控设备什么都不用装，只要SSH能连上就行。

```bash
# 1. 把整个目录拷到部署服务器
scp -r traffic_monitor/ root@服务器IP:/data/

# 2. 安装sshpass（离线，不需要外网）
cd /data/traffic_monitor
dpkg -i sshpass_jammy.deb        # Ubuntu 22.04
# 或
dpkg -i sshpass.deb              # Ubuntu 24.04+

# 3. 编辑设备配置，写上所有需要监控的设备IP、密码、网卡名
vim devices.json

# 4. 启动
nohup python3 monitor.py > /tmp/traffic_monitor.log 2>&1 &

# 5. 浏览器访问
http://服务器IP:18888/
```

## 配置文件 devices.json

每行一台设备，有多少加多少：

```json
[
  {"name": "流量节点1", "ip": "10.1.1.1", "iface": "ens160", "user": "root", "password": "xxx"},
  {"name": "流量节点2", "ip": "10.1.1.2", "iface": "ens192", "user": "root", "password": "xxx"},
  {"name": "数据节点1", "ip": "10.1.2.1", "iface": "ens160", "user": "root", "password": "xxx"},
  {"name": "管理节点",  "ip": "10.1.3.1", "iface": "ens160", "user": "root", "password": "xxx"}
]
```

| 字段 | 说明 | 必填 |
|------|------|------|
| name | 页面显示的名字，随意取 | 是 |
| ip | 设备IP地址 | 是 |
| iface | 默认监控的网卡名 | 是 |
| user | SSH用户名，默认root | 否 |
| password | SSH密码 | 是 |

修改 `devices.json` 后需重启程序生效。

## 页面功能

### 实时监控页（首页）

- 左侧设备列表：名称、IP/接口、在线状态、实时速率
- 右侧折线图：入站/出站流量趋势
- 接口选择下拉框：切换监控不同网卡
- 入站/出站/全部按钮：切换图表显示模式
- 时间范围：10分钟 / 1小时 / 6小时 / 24小时 / 7天
- 自动5秒刷新

### 报表页（点击"报表"按钮）

- 汇总：所有设备均值合计、峰值
- 每设备卡片：在线状态、入站均值/峰值、出站均值/峰值、入站/出站总量、折线图
- 打印/导出PDF功能

## 运行管理

```bash
# 启动
nohup python3 /data/traffic_monitor/monitor.py > /tmp/traffic_monitor.log 2>&1 &

# 停止
pkill -f monitor.py

# 查看日志
tail -f /tmp/traffic_monitor.log

# 开机自启（可选）
(crontab -l 2>/dev/null; echo "@reboot python3 /data/traffic_monitor/monitor.py") | crontab -

# 取消开机自启
crontab -l | grep -v monitor.py | crontab -
```

### 彻底卸载

```bash
# 1. 停程序
pkill -f monitor.py

# 2. 删目录（程序、配置、数据库全删）
rm -rf /data/traffic_monitor/

# 3. 删sshpass（如果不需要了）
dpkg -r sshpass

# 4. 取消开机自启（如果配过）
crontab -l | grep -v monitor.py | crontab -
```

> 卸载后对其他设备没有任何影响，程序只是通过SSH读取网卡统计数据，不修改任何设备上的任何东西。

## 运行要求

### 部署服务器（跑程序的机器）

```bash
# Python 3 — Ubuntu自带，不需要额外安装
python3 --version    # 确认有Python 3.6+
```

**SSH认证：密码认证（需要sshpass）**
```bash
# 程序自带 sshpass.deb，离线安装即可，无需外网
dpkg -i sshpass_jammy.deb        # Ubuntu 22.04 (Jammy)
dpkg -i sshpass.deb              # Ubuntu 24.04+

# devices.json 里写 password 字段
```

### 被监控设备

- Linux系统（读 `/sys/class/net/` 统计数据）
- 开启SSH服务（`systemctl status sshd`）
- 不需要装任何额外软件

### 浏览器

- 任意现代浏览器，无JS依赖要求

> 程序纯Python标准库实现，不需要pip安装任何包。Ubuntu自带Python3，离线装一个sshpass就行。

## 资源占用

| 设备数 | CPU | 内存 | 数据库(30天) | SSH连接 |
|--------|-----|------|-------------|---------|
| 3台 | ~0.2% | ~20MB | ~2MB | 3 |
| 10台 | ~1% | ~40MB | ~7MB | 10 |
| 30台 | ~2% | ~100MB | ~20MB | 30 |
| 100台 | ~5% | ~300MB | ~70MB | 100 |

- 实时数据1秒采样（内存），数据库10秒写一次
- SSH为常驻长连接，断线自动重连
- 数据库30天自动清理

## 文件说明

| 文件 | 说明 |
|------|------|
| monitor.py | 主程序，单文件，含后端+前端 |
| devices.json | 设备配置，首次运行自动生成模板 |
| traffic.db | SQLite数据库，自动创建，存储历史数据 |
| sshpass_jammy.deb | sshpass安装包（Ubuntu 22.04），密码认证时需要 |
| sshpass.deb | sshpass安装包（Ubuntu 24.04+），密码认证时需要 |
| README.md | 本文档 |

## 常见问题

**设备显示"离线"**
- 检查SSH能否连通：`ssh root@设备IP`
- 检查sshpass是否安装：`which sshpass`
- 检查密码是否正确

**接口下拉框为空**
- 检查目标设备的 `/sys/class/net/` 是否可访问
- SSH连接超时可能导致接口列表获取失败

**想监控多台设备的不同接口**
- 在首页选中设备后切换接口即可，切换会自动持久化
- 也可以在 devices.json 里给同一IP写多条配置（name取不同名）

**数据库太大**
- 采样间隔默认10秒，可在 monitor.py 中修改 `DB_SAMPLE_INTERVAL`
- 保留天数默认30天，可修改 `RETENTION_DAYS`