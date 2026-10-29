"""Interactive local deployment helper. Never enables DNS writes or publishes images."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import quote, urlsplit
from .common import request

ROOT = Path(__file__).resolve().parent.parent


def run(args, capture=False):
    environment = dict(os.environ)
    if args[:2] == ['docker', 'compose'] and '-f' in args:
        for key in ('COMPOSE_PROJECT_NAME','COMPOSE_FILE','COMPOSE_ENV_FILES','COMPOSE_PROFILES'):
            environment.pop(key, None)
        config = Path(args[args.index('-f')+1]).parent/'.env'
        if config.exists():
            for key in read_env(config):
                environment.pop(key, None)
    result = subprocess.run(args, text=True, capture_output=True, env=environment)
    if result.returncode:
        # Do not echo subprocess diagnostics containing environment values or URLs.
        raise RuntimeError('命令执行失败：' + ' '.join(args[:2]) + '；请检查 Docker 状态和网络。')
    if capture:
        return result.stdout
    if result.stdout.strip():
        print(result.stdout.strip())


def ask(label, default='', hidden=False):
    suffix = ' [' + default + ']' if default and not hidden else ''
    value = (getpass.getpass if hidden else input)(label + suffix + ': ').strip()
    return value or default


def ipv4_list(value):
    ips = [str(ipaddress.IPv4Address(x.strip())) for x in value.split(',')]
    if len(ips) < 2 or len(set(ips)) != len(ips):
        raise ValueError('至少填写两个不同的入口 IPv4 地址。')
    return ','.join(ips)


def port(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('端口必须在 1–65535 之间。')
    return str(int(value))


def private_bind(value):
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')):
        raise ValueError('监听地址必须为本机私网、回环或 Tailscale IPv4，不能是 0.0.0.0 或公网地址。')
    return str(address)


def image_ref(value, role):
    if not value:
        return 'ip-guard-' + role + ':local'
    if (not re.fullmatch(r'[a-z0-9][a-z0-9._/:@-]+', value) or '/' not in value
            or (':' not in value.rsplit('/', 1)[-1])):
        raise ValueError('请输入带明确版本 tag 或 digest 的完整镜像名。')
    if value.endswith(':latest'):
        raise ValueError('请使用明确版本，避免 latest 在更新时改变内容。')
    return value


def read_env(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        result[key] = value[1:-1] if value.startswith("'") and value.endswith("'") else value
    return result


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)


def env_text(values):
    for key, value in values.items():
        if not re.fullmatch('[A-Z][A-Z0-9_]*', key) or not isinstance(value, str) or any(c in value for c in "\n\r'\\"):
            raise ValueError('配置值含不支持的换行、引号或反斜线。')
    return ''.join(key + "='" + value + "'\n" for key, value in values.items())


def cloudflare_record(values):
    token = values['CF_API_TOKEN']
    if not token:
        return ''
    zone = values['CF_ZONE_ID']
    if not re.fullmatch('[a-fA-F0-9]{32}', zone):
        raise ValueError('Zone ID 应为 32 位十六进制字符串。')
    data = request('https://api.cloudflare.com/client/v4/zones/' + zone + '/dns_records?name=' +
                   quote(values['DNS_NAME'], safe='') + '&per_page=100',
                   headers={'Authorization': 'Bearer ' + token})
    records = data.get('result')
    if data.get('success') is not True or not isinstance(records, list) or len(records) != 1:
        raise ValueError('Cloudflare 读取失败或存在多个同名记录。')
    record = records[0]
    if (record.get('name') != values['DNS_NAME'] or record.get('type') != 'A'
            or record.get('proxied') is not False or record.get('content') not in values['ENTRY_IPS'].split(',')):
        raise ValueError('DNS 必须是指向入口之一的单个 DNS-only A 记录。')
    print('Cloudflare 只读验证成功，Record ID 已自动识别。')
    return record['id']


def notifications(values, controller):
    values['FEISHU_WEBHOOK'] = ask('飞书 Webhook（回车跳过）', hidden=True)
    if values['FEISHU_WEBHOOK']:
        if not values['FEISHU_WEBHOOK'].startswith('https://open.feishu.cn/open-apis/bot/v2/hook/'):
            raise ValueError('飞书 Webhook 地址格式错误。')
        values['FEISHU_SIGNING_SECRET'] = ask('飞书签名密钥（未启用则回车）', hidden=True)
    if controller:
        values['TELEGRAM_BOT_TOKEN'] = ask('Telegram Bot Token（回车跳过）', hidden=True)
        if values['TELEGRAM_BOT_TOKEN']:
            values['TELEGRAM_CHAT_ID'] = ask('Telegram Chat ID')
            if not values['TELEGRAM_CHAT_ID']:
                raise ValueError('配置 Telegram Bot 时需要 Chat ID。')


def compose(role):
    return ['docker','compose','-f',str(ROOT/'deploy'/role/'compose.yaml')]


def prepare_image(role, reference):
    image = image_ref(reference, role)
    if reference:
        print('正在拉取指定版本镜像……')
        run(['docker','pull',image])
    else:
        print('尚未选择已发布镜像，正在从本机源码构建（可能需要几分钟）……')
        run(['docker','build','--target',role,'-t',image,str(ROOT)])
    return image


def configure_controller(values):
    values['ENTRY_IPS'] = ipv4_list(ask('入口 IPv4，按优先级用逗号分隔'))
    from .probe import source_map, targets_config, DEFAULT_TARGETS
    sources = ask('对应的本机私网 IPv4，顺序与入口一致（ip -4 addr 可查看）')
    source_map(sources, values['ENTRY_IPS'].split(','))
    values['SOURCE_IPS'] = sources
    domain = ask('需要切换的完整 DNS 名称').lower().rstrip('.')
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}',domain):
        raise ValueError('DNS 名称格式错误。')
    values['DNS_NAME'] = domain
    values['CONTROLLER_BIND_IP'] = private_bind(ask('监控监听私网 IP（仅本机使用保留默认）','127.0.0.1'))
    print('默认探测阿里云北京、杭州、深圳 TCP 443；不代表三大运营商覆盖。')
    values['PROBE_TARGETS'] = ask('探测目标 JSON（回车使用默认）',json.dumps(DEFAULT_TARGETS,separators=(',',':')))
    targets_config(values['PROBE_TARGETS'])
    values['ADMIN_TOKEN'] = secrets.token_hex(32)
    values['CF_API_TOKEN'] = ask('Cloudflare DNS Edit Token（回车稍后配置）',hidden=True)
    if values['CF_API_TOKEN']:
        values['CF_ZONE_ID'] = ask('Cloudflare Zone ID')
        values['CF_RECORD_ID'] = cloudflare_record(values)
    notifications(values, True)
    values['DRY_RUN'], values['ENABLE_DNS_WRITES'] = 'true', ''
    return values


def initialize_data(role, image, folder):
    data = folder/'data'
    data.mkdir(exist_ok=True)
    code = "import os; os.chown('/data',10001,10001); os.chmod('/data',0o700)"
    run(['docker','run','--rm','--network','none','--user','0','--read-only','--cap-drop','ALL',
         '--cap-add','CHOWN','--cap-add','FOWNER','--cap-add','DAC_OVERRIDE','-v',str(data)+':/data',image,'python','-c',code])


def status(role):
    run(compose(role)+['ps'])
    run(compose(role)+['exec','-T','controller','python','-m','guard.admin','status'])


def setup(role):
    folder = ROOT/'deploy'/role
    if (folder/'.env').exists() or (folder/'data').exists():
        raise ValueError('已有配置或运行数据，安装向导不会覆盖。请使用 status/start，或按运维文档手动修改。')
    if run(['docker','ps','-aq','--filter','label=com.docker.compose.project=ip-guard-'+role],capture=True).strip():
        raise ValueError('Docker 中已有此角色的项目容器，请先定位原部署；安装向导不会接管或覆盖它。')
    values = read_env(folder/'.env.example')
    reference = ask('已发布镜像完整名称（见 docs/publishing.md；回车从源码构建）')
    values['IP_GUARD_IMAGE'] = reference
    image_ref(reference,role)
    configure_controller(values)
    image = prepare_image(role,reference)
    text = env_text(values)
    write_private(folder/'.env',text)
    initialize_data(role,image,folder)
    run(compose(role)+['config','--quiet'])
    run(compose(role)+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','180'])
    print('单机服务已启动，默认 Dry Run；请核对 source_mapping_verified、区域成功率及通知。')
    status(role)


def main():
    parser=argparse.ArgumentParser(description='OCI IP Guard 本机部署助手（默认 Dry Run，不发布镜像）')
    parser.add_argument('command',choices=['setup','start','status','logs','test-notification','check','events','switch','reconcile'])
    parser.add_argument('target',nargs='?',help='仅 switch 命令使用：目标公网 IP')
    args=parser.parse_args()
    args.role = 'controller'
    if (args.command == 'switch') != bool(args.target):
        parser.error('switch 需要目标公网 IP；其他命令不接受额外参数')
    try:
        if not sys.platform.startswith('linux'):
            raise ValueError('部署要求 Linux；需要 host 网络绑定本机私网源地址。')
        docker_host = os.environ.get('DOCKER_HOST')
        if os.environ.get('DOCKER_CONTEXT') or not docker_host:
            context = [os.environ['DOCKER_CONTEXT']] if os.environ.get('DOCKER_CONTEXT') else []
            docker_host = json.loads(run(['docker','context','inspect',*context,'--format','{{json .Endpoints.docker.Host}}'],capture=True))
        if not docker_host.startswith('unix://'):
            raise ValueError('安装助手只支持本机 Docker Unix socket，不支持远端 Docker context。')
        security = json.loads(run(['docker','info','--format','{{json .SecurityOptions}}'],capture=True))
        if any('rootless' in item for item in security):
            raise ValueError('绑定主机源地址需要标准 rootful Docker；当前 rootless 模式不受支持。')
        run(['docker','compose','version'],capture=True)
        if args.command=='setup':
            setup(args.role)
        elif args.command=='start':
            folder=ROOT/'deploy'/args.role
            values=read_env(folder/'.env')
            initialize_data(args.role,image_ref(values.get('IP_GUARD_IMAGE',''),args.role),folder)
            run(compose(args.role)+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','180'])
            status(args.role)
        elif args.command=='status':
            status(args.role)
        elif args.command=='logs':
            run(compose(args.role)+['logs','--tail','80'])
        elif args.command=='check':
            run(compose(args.role)+['exec','-T','controller','python','-m','guard.probe'])
        else:
            command = ['docker','compose','-f',str(ROOT/'deploy/controller/compose.yaml'),
                       'exec','-T','controller','python','-m','guard.admin',args.command]
            if args.target:
                command.append(str(ipaddress.IPv4Address(args.target)))
            run(command)
            if args.command == 'test-notification':
                print('测试事件已入队；请检查实际接收渠道，入队不等于送达。')
    except (Exception, KeyboardInterrupt) as error:
        # Network exceptions can contain API/webhook URLs, so show only safe validation messages.
        detail = str(error) if type(error) in (ValueError,RuntimeError) else type(error).__name__
        print('未完成：'+detail+'\n已存在的配置和服务不会自动删除。',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
