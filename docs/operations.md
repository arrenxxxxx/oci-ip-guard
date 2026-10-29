# 运维与监控

## 本地查看

无需监控平台，直接运行：

```sh
./ip-guard status  # 当前状态
./ip-guard events  # 本地事件
./ip-guard logs    # 运行日志
```

## 接口和状态

- `/healthz`：决策/通知工作线程与本地探测心跳。探测断网结果仍是有效的“完成一次检测”，具体故障查看指标。
- `/metrics`：Prometheus 指标；监听回环和配置的单个私网 IP。
- `/status`、`/events`、`/switch`、`/reconcile`、`/test-notification`：需要 `Authorization: Bearer <ADMIN_TOKEN>`，推荐通过 `./ip-guard` 操作。
- `/report`：已关闭，返回 410，旧 NAS 上报不会进入决策。

SQLite 数据在 `deploy/controller/data/guard.db`，保存事件、通知重试、当前入口和切换时间。不要在有未确认 DNS 写入时删库；先确认实际 DNS，再运行 reconcile，重新等待健康样本与冷却。飞书和 Telegram 失败独立重试，日志不打印 Token 或 Webhook。

单次目标失败保留在成功比例分母中；本地地址不存在、DNS 解析异常属于系统异常，整轮不参与决策。探测长时间未完成会触发本地心跳过期告警并禁止切换。默认两个海外 TCP 对照点是 1.1.1.1:443 和 8.8.8.8:443，使用默认出口。

## Prometheus / Grafana（可选）

本节可跳过。巡检、记录、通知与自动切换均不依赖这些服务；需要图表时再接入。服务提供 `/metrics` 供 Prometheus 主动采集，Grafana 读取 Prometheus 展示数据。

将 `monitoring/prometheus-job.yaml` 的 scrape job 合入已有 Prometheus，把目标替换为服务器私网 IP:8765。若主机防火墙拒绝 Docker 到宿主机访问，应仅放行监控网桥到所选私网监听地址的 TCP 8765，并按现有防火墙管理方式持久化；不要对公网放行。Docker host 模式不会提供 `controller` 或 `oci-ip-guard` 容器 DNS 别名。导入 `monitoring/dashboard.json`，其数据源 UID 默认为 `prometheus`，按已有环境调整。可选规则在 `monitoring/alerts.yaml`；已有 Grafana Telegram 联系点需要配置相应告警规则和路由。

看板包括区域成功率、每地区连通性和延迟、当前 DNS IP、故障次数、切换事件、有效样本心跳及通知积压。成功率是目标计数比例；历史可用率是整轮达到健康阈值的比例。

为迁移旧看板暂时保留 `oci_guard_nas_online` 和 `oci_guard_nas_heartbeat_timestamp_seconds` 指标别名，含义已经变为本地探测，不表示仍有 NAS。

## 故障模拟（不触碰真实 DNS）

```sh
python3 -m unittest discover -s tests -v
```

测试使用模拟 Cloudflare 客户端验证：连续三轮低成功比例才切换、灰区重置连续次数、备用未达到阈值禁止切换、全故障/网络异常/源地址映射错误/数据过期不切换、冷却、不自动切回、DNS 写入不确定后禁止重试、重启状态恢复、通知独立重试、Dry Run 不写 DNS。真实探测用 `./ip-guard check`，它只输出样本，不提交决策。

## 从双机版迁移

先保存旧 SQLite 数据与私有 `.env`，配置源地址和区域目标，使用新镜像在 host 网络运行；复用 ADMIN_TOKEN、Cloudflare 和通知凭据。旧上报密钥不再使用。Prometheus 的抓取地址由容器别名改为主机私网地址。确认连续至少三轮采样、DNS 只读核验及通知正常后，停止 NAS 的 agent/relay 容器。不要直接在有业务的目录运行清理命令。

## 日志与更新

Docker 日志轮转为 3 × 10MB。SQLite 事件保留历史，按需求监控磁盘使用。更新前保存私有配置和数据库；拉取明确版本或本地重建镜像后运行 `./ip-guard start`。修改区域目标或阈值会重建容器，并重新累计连续次数。
