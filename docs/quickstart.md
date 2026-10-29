# 单机安装

## 1. 确认地址

在 OCI 控制台确认每个公网入口对应的私网 IP，并通过 `ip -4 addr` 确认私网地址已分配到本机。项目只使用这些地址发起连接，不修改网络配置。

服务采用 Docker `network_mode: host`，普通非 root UID 10001，删除全部 Linux capabilities。原始 TCP socket 显式绑定私网源地址，不读取 HTTP/SOCKS 代理变量。每五分钟用两个 HTTPS 公网回显服务核对映射；映射不一致或无法验证会禁止切换。上游网络及本机透明代理仍需管理员自行核验。

## 2. 向导启动

```sh
./ip-guard setup
```

填写候选公网 IP（按优先级）、同顺序的本机私网 IP、DNS 域名、监听私网地址、区域目标及凭据。默认监听回环；如已有 Docker Prometheus，填写其可访问的宿主机私网地址。回环始终提供健康与管理接口。不需要给公网开放 8765。

向导生成 `deploy/controller/.env`。已有 `.env`、数据目录或同名项目时不会覆盖。启动异常可以保留配置修复后运行 `./ip-guard start`。

Cloudflare Token 权限为指定 Zone 的 Zone → DNS → Edit，避免全局 API Key。填写 Zone ID 后向导只读查找 Record ID；记录必须是唯一、DNS-only、内容为候选 IP 之一的 A 记录。若未填写 Token，只能使用 DNS 解析进行 Dry Run。Token 的 DNS Edit 权限最终仍需实际写操作才能验证。

通知按需配置，可使用 Telegram、飞书 Webhook 或同时启用；两者均跳过时仍会在本地记录事件。飞书 Hook、签名 Secret、Telegram Token/Chat ID 均输入到向导或私有 `.env`，不要提交 Git。若 Telegram 由现有 Grafana 联系点转发，配置好对应告警规则后设置 `TELEGRAM_VIA_GRAFANA=true`，该标志本身不会创建联系点。

## 3. 手动 Compose 安装

```sh
cp deploy/controller/.env.example deploy/controller/.env
chmod 600 deploy/controller/.env
# 编辑 .env，生成 ADMIN_TOKEN：openssl rand -hex 32
mkdir -p deploy/controller/data
sudo chown 10001:10001 deploy/controller/data
sudo chmod 700 deploy/controller/data
docker compose -f deploy/controller/compose.yaml up -d --build
```

`ENTRY_IPS` 与 `SOURCE_IPS` 必须等长且一一对应。`PROBE_TARGETS` 是对象数组，每项包含唯一 `name`、`host` 和 TCP `port`；使用 3–12 个稳定、允许低频探测的目标。相同端点不可重复计票，名称只允许字母、数字、下划线和短横线。主机名只解析为公网 IPv4，各入口使用当轮相同目标地址。解析失败属于未知状态，不会从分母剔除后虚增成功率。

## 4. 观察后再启用

```sh
./ip-guard status
./ip-guard check
./ip-guard test-notification
```

核对 `direct_ok`（映射验证）、`internet_ok`（默认出口对照）、区域结果和真实通知接收情况。默认 `DRY_RUN=true`；经人工确认稳定后才同时设置：

```dotenv
DRY_RUN=false
ENABLE_DNS_WRITES=I_HAVE_REVIEWED_DRY_RUN
```

执行 `./ip-guard start` 重新创建服务。重启会清空连续判定次数，要求重新获取新鲜样本。建议先保留 Dry Run，完成观察后再启用。
