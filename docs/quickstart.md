# 单机安装

## 1. 确认地址

在 OCI 控制台确认每个公网入口对应的私网 IP，并通过 `ip -4 addr` 确认私网地址已分配到本机。项目只使用这些地址发起连接，不修改网络配置。

服务采用 Docker `network_mode: host`，普通非 root UID 10001，删除全部 Linux capabilities。原始 TCP socket 显式绑定私网源地址，不读取 HTTP/SOCKS 代理变量。每五分钟用两个 HTTPS 公网回显服务核对映射；映射不一致或无法验证会禁止切换。上游网络及本机透明代理仍需管理员自行核验。

## 2. 向导启动

```sh
./ip-guard setup
```

首先明确选择运行模式：

- **真实自动切换**：故障满足阈值且所有保护条件允许时修改 Cloudflare DNS；完成检查后还必须再次确认写入。
- **模拟观察模式**：继续探测、记录并通知，但故障时不会修改 DNS。

没有默认选择，直接回车不会静默进入模拟模式。

填写候选公网 IP（按优先级）、同顺序的本机私网 IP、DNS 域名、监听私网地址、区域目标及凭据。默认监听回环；如已有 Docker Prometheus，填写其可访问的宿主机私网地址。回环始终提供健康与管理接口。不需要给公网开放 8765。

向导生成 `deploy/controller/.env`。已有 `.env`、数据目录或同名项目时不会覆盖。启动异常可以保留配置修复后运行 `./ip-guard start`。

Cloudflare Token 使用指定 Zone 的 Zone → DNS → Edit，并提供只读 Zone 和自身 Token policy 查询所需权限，避免全局 API Key。运行时只读调用 Token verify、自身 Token policy、Zone 和 DNS 记录接口，核对有效状态、DNS Write 权限及 Zone scope；无法读取自身 policy（例如缺少 API Tokens Read）时自动切换不可用。请在 Cloudflare 中人工检查并使用可验证、最小范围的 Token；不要扩大 Zone 范围来绕过校验。填写 Zone ID 后向导只读查找 Record ID；记录必须是唯一、DNS-only、内容为候选 IP 之一的 A 记录。模拟观察模式可以不填写 Token，此时仅使用 DNS 解析作为当前入口证据。真实模式不能缺少 Token、Zone ID 或 Record ID，且必须取得只读权限证据；不能取得证据时显示自动切换不可用，而不是尝试修改 DNS 来测试权限。检查包含唯一 A 记录、DNS-only、候选 IP、源地址映射、检测链路和健康备用入口。只读权限证据不保证将来的网络请求一定成功，真正切换仍会检查执行结果并读回核验。

通知按需配置，可使用 Telegram、飞书 Webhook 或同时启用；两者均跳过时仍会在本地记录事件。飞书 Hook、签名 Secret、Telegram Token/Chat ID 均输入到向导或私有 `.env`，不要提交 Git。若 Telegram 由现有 Grafana 联系点转发，配置好对应告警规则后设置 `TELEGRAM_VIA_GRAFANA=true`，该标志本身不会创建联系点。

## 3. 手动 Compose 安装

```sh
cp deploy/controller/.env.example deploy/controller/.env
chmod 600 deploy/controller/.env
# 编辑 .env，明确设置 RUN_MODE=live 或 RUN_MODE=simulate
# live 还需 ENABLE_DNS_WRITES=I_HAVE_REVIEWED_DRY_RUN 并通过检查
# 生成 ADMIN_TOKEN：openssl rand -hex 32
mkdir -p deploy/controller/data
sudo chown 10001:10001 deploy/controller/data
sudo chmod 700 deploy/controller/data
docker compose -f deploy/controller/compose.yaml up -d --build
```

`ENTRY_IPS` 与 `SOURCE_IPS` 必须等长且一一对应。`PROBE_TARGETS` 是对象数组，每项包含唯一 `name`、`host` 和 TCP `port`；使用 3–12 个稳定、允许低频探测的目标。相同端点不可重复计票，名称只允许字母、数字、下划线和短横线。主机名只解析为公网 IPv4，各入口使用当轮相同目标地址。解析失败属于未知状态，不会从分母剔除后虚增成功率。

## 4. 模式选择与旧配置迁移

新 `.env.example` 的 `RUN_MODE` 故意留空，直接复制而不选择模式会报告未配置并禁止自动切换，不会悄悄作为已配置的模拟模式运行。不要在 Compose 的 `environment` 中添加模式默认值覆盖 `.env`；本项目通过 `env_file: .env` 原样加载。

真实自动切换配置（必须由操作人主动填写）：

```dotenv
RUN_MODE=live
ENABLE_DNS_WRITES=I_HAVE_REVIEWED_DRY_RUN
```

该确认字符串为兼容旧部署保留，不要求先运行模拟模式。填写它表示已审核并允许当前指定记录的 DNS 写入，不是权限检查结果，也不能绕过保护条件。还需完整 Cloudflare 凭据及有效公网/私网映射、探测配置。

只观察、不写 DNS：

```dotenv
RUN_MODE=simulate
ENABLE_DNS_WRITES=
```

运行后核对：

```sh
./ip-guard status
./ip-guard check
./ip-guard test-notification
```

确认映射、默认出口对照、区域结果、备用入口和真实通知接收。容器运行正常不等于探测结果正常，更不等于 DNS 自动切换可用。冷却、未取得连续健康样本、无健康备用、映射异常等都会阻止切换，查看状态中的具体原因；不要靠切换 DNS 来验收写权限。

**升级已有部署不会自动开启 DNS 写入。**

- 备份私有 `.env` 和 SQLite 数据，保留原模式，不用新示例覆盖原文件。
- 没有 `RUN_MODE` 的旧配置仍按原有 `DRY_RUN` 和写入确认配置解释；`DRY_RUN=true` 永远保持模拟。原来未设置模式的安全默认行为也不会升级为真实写入。
- 迁移旧模拟配置时主动填写 `RUN_MODE=simulate`，删除旧 `DRY_RUN` 行，并清空写入确认值。
- 只有打算开启真实切换时才改成上述 `live` 配置；如果仍保留 `DRY_RUN`，必须与明确模式一致。冲突配置报告异常并禁止写入，绝不优先选择真实模式。
- 旧真实模式仍需已有确认值；新的只读校验失败时保持禁止写入并报告原因，需要修复权限、记录或探测问题，不能靠升级绕过。

修改后按现有镜像更新流程执行 `./ip-guard start` 重新创建服务。重启清空连续判定次数，重新获取新鲜样本。镜像中必须包含本次修复；仅编辑示例文件不能改变旧镜像行为。
