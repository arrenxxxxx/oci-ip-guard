# 运维与监控

## 本地查看

无需监控平台，直接运行：

```sh
./ip-guard status  # 当前状态
./ip-guard events  # 本地事件
./ip-guard logs    # 运行日志
```

## 接口和状态

- `/healthz`：决策/通知工作线程与本地探测心跳，反映进程存活，不代表 DNS 自动切换可用。探测断网结果仍是有效的“完成一次检测”；同时查看返回的运行模式和自动切换状态。
- `/metrics`：Prometheus 指标；监听回环和配置的单个私网 IP。
- `/status`、`/events`、`/switch`、`/reconcile`、`/test-notification`：需要 `Authorization: Bearer <ADMIN_TOKEN>`，推荐通过 `./ip-guard` 操作。
- `/report`：已关闭，返回 410，旧 NAS 上报不会进入决策。

`/status` 的 `auto_switch` 提供 `available`、`state`、`message` 和 `reasons`（每项有稳定 `code` 与说明 `message`）。只看到容器 healthy、探测完成或 `dns_read_ok=1`，都不足以证明 DNS 能切换。

- **DNS 自动切换已启用**：真实模式、已确认写入、只读权限证据和当前安全条件均满足。仍需当前入口达到故障阈值才执行，不会为演示而切换。
- **模拟模式：故障时不会修改 DNS**：即使所有探测与凭据正常，也绝不会写入。
- **自动切换不可用**：检查 `reasons`；修复凭据或权限、唯一 DNS-only A 记录及候选内容、源地址映射或探测链路，或等待新鲜连续健康样本与冷却结束。未确认写入意图必须先人工核对 DNS，再 reconcile。

只读权限检查不能证明将来请求必定成功；它不会通过一次“写回原值”或临时切换测试权限。Token policy 不可读、未知权限或无法明确匹配 Zone scope 均按不可用处理。绝不根据 Token 名称或记录读取成功推断 DNS Write 权限。

SQLite 数据在 `deploy/controller/data/guard.db`，保存事件、通知重试、当前入口和切换时间。不要在有未确认 DNS 写入时删库；先确认实际 DNS，再运行 reconcile，重新等待健康样本与冷却。飞书和 Telegram 失败独立重试，日志不打印 Token 或 Webhook。

单次目标失败保留在成功比例分母中；本地地址不存在、DNS 解析异常属于系统异常，整轮不参与决策。探测长时间未完成会触发本地心跳过期告警并禁止切换。默认两个海外 TCP 对照点是 1.1.1.1:443 和 8.8.8.8:443，使用默认出口。

## Prometheus / Grafana（可选）

本节可跳过。巡检、记录、通知与自动切换均不依赖这些服务；需要图表时再接入。服务提供 `/metrics` 供 Prometheus 主动采集，Grafana 读取 Prometheus 展示数据。

将 `monitoring/prometheus-job.yaml` 的 scrape job 合入已有 Prometheus，把目标替换为服务器私网 IP:8765。若主机防火墙拒绝 Docker 到宿主机访问，应仅放行监控网桥到所选私网监听地址的 TCP 8765，并按现有防火墙管理方式持久化；不要对公网放行。Docker host 模式不会提供 `controller` 或 `oci-ip-guard` 容器 DNS 别名。导入 `monitoring/dashboard.json`，其数据源 UID 默认为 `prometheus`，按已有环境调整。可选规则在 `monitoring/alerts.yaml`；已有 Grafana Telegram 联系点需要配置相应告警规则和路由。

看板顶部优先显示「DNS 自动切换实际状态」：绿色为已启用，橙色为模拟观察（不会修改 DNS），红色为不可用；无数据明确显示「无当前数据，不能判断」。另列控制器线程、有效探测、只读写权限证据与阻止原因，避免把存活与容灾能力混淆。每个实例保留独立标签，多个控制器不要混合解读。

关键指标：

| 指标 | 含义 |
| --- | --- |
| `oci_guard_mode_live` / `oci_guard_mode_simulate` | 配置的实际运行模式，1 为是 |
| `oci_guard_auto_switch_available` | 当前所有自动切换前置条件已满足，1 为可用；不表示发生过切换 |
| `oci_guard_dns_write_capable` | 只读证据确认指定 Zone 的 DNS 写入能力，非真实写入测试 |
| `oci_guard_auto_switch_blocked{reason="..."}` | 当前保护原因，1 为阻止；详细修复信息见 status |
| `oci_guard_worker_healthy` / `oci_guard_probe_worker_healthy` | 分别为控制器工作线程、探测线程心跳存活，与切换可用性分开 |
| `oci_guard_sample_valid` | 当前探测样本有效，不能单独证明 DNS 写入可用 |

原 `oci_guard_dry_run` 保留兼容；不能用它等于 0 就判断切换已启用。

看板还包括区域成功率、每地区连通性和延迟、当前 DNS IP、故障次数、切换事件、有效样本心跳及通知积压。成功率是目标计数比例；历史可用率是整轮达到健康阈值的比例。

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

## 通知结果怎么区分

普通入口故障告警只代表检测到故障，不代表 DNS 已修改。查看相应结果事件：

- `dns_switch`：原 IP → 新 IP，API 读回已确认；这是实际切换成功证据。
- `dry_run_switch`：检测到切换条件，但当前为模拟模式，DNS 未修改。
- `switch_blocked`：保护条件阻止本次新写入，说明具体原因；如果已有 pending，不能据此断定实际 DNS 未改变。
- `dns_write_failed`：DNS 写入失败或结果不确定。先查看当前记录和 pending 状态，不要盲目重试；请求超时不能保证服务端未执行。

Telegram、飞书使用现有事件队列独立重试；Grafana 可使用 `monitoring/alerts.yaml` 的分类型规则和事件计数。Grafana 规则保留事件类型，但原/新 IP 与详细结果以本地事件及直接渠道通知为准；计数告警不是逐事件消息队列，稀疏抓取可能漏掉瞬时增量。`TELEGRAM_VIA_GRAFANA=true` 只标记外部已配置，不会替你创建联系点或路由。

模拟模式与切换不可用规则会主动暴露状态，避免长期误以为容灾生效。若有意长期模拟，可在监控平台对对应实例的模拟提醒设置静默；不要把自动切换不可用告警一并禁用。
