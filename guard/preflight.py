"""Read-only deployment validation. Never invokes Cloudflare.switch()."""
import json
from .cloudflare import Cloudflare
from .probe import Prober


def check(dns, prober):
    current = dns.preflight()
    report = prober.cycle()
    if not report['direct_ok']:
        raise ValueError('公网入口与本机源地址映射未验证；检查 ENTRY_IPS / SOURCE_IPS 顺序、本机地址及两条 HTTPS 回显链路。')
    if not report['internet_ok']:
        raise ValueError('默认出口控制探测失败；检查现有出口网络与控制目标连通性，不要修改固定出口策略。')
    if not any(ip != current and sample['up'] for ip, sample in report['samples'].items()):
        raise ValueError('没有健康备用入口；检查备用源地址及 PROBE_TARGETS。运行时仍须累计规定的连续健康轮次。')
    return {'ok': True, 'reasons': [], 'read_only': True, 'current_ip': current, 'dns_record_ready': True,
            'mapping_ok': True, 'internet_ok': True,
            'message': '只读预检通过；尚未启用 DNS 写入，启动后仍须满足连续探测与冷却保护。'}


def main():
    try:
        print(json.dumps(check(Cloudflare(False), Prober()), ensure_ascii=False))
        return 0
    except Exception as error:
        # Do not expose credential-bearing URLs or arbitrary remote diagnostics.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__ + '；检查凭据、源地址和网络后重试。'
        print(json.dumps({'ok': False, 'reasons': [detail], 'read_only': True, 'error': detail, 'message': '自动切换不可用'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
