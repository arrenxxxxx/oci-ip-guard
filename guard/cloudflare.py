import os
import re
import socket
import urllib.parse
from .common import DOMAIN, IPS, request
from .mode import resolve_mode


class DNSWriteBlocked(RuntimeError):
    """The request was rejected locally before any DNS PATCH was attempted."""


class Cloudflare:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.write_confirmed = not resolve_mode(os.environ).dry_run
        self.token = os.environ.get('CF_API_TOKEN', '')
        self.zone = os.environ.get('CF_ZONE_ID', '')
        self.record = os.environ.get('CF_RECORD_ID', '')
        self.configured = bool(self.token and self.zone and self.record)
        self.write_capable = False
        self.preflight_error = ''

    def api(self, path, payload=None, method=None):
        result = request('https://api.cloudflare.com/client/v4' + path, payload,
                         {'Authorization': 'Bearer ' + self.token}, method)
        if result.get('success') is not True:
            raise RuntimeError('Cloudflare API rejected request')
        return result['result']

    @staticmethod
    def policy_allows_dns_write(details, zone, account):
        """Only accept unambiguous allow policies; unknown shapes fail closed."""
        policies = details.get('policies')
        if not isinstance(policies, list) or not policies:
            return False
        # A deny/unknown effect might override any allow. Do not guess its scope.
        if any(not isinstance(p, dict) or p.get('effect') != 'allow' for p in policies):
            return False
        for policy in policies:
            groups = policy.get('permission_groups', [])
            if not isinstance(groups, list):
                continue
            if not any(g.get('name') == 'DNS Write'
                       for g in groups if isinstance(g, dict)):
                continue
            resources = policy.get('resources', {})
            if not isinstance(resources, dict):
                continue
            if resources.get('com.cloudflare.api.account.zone.' + zone) == '*':
                return True
            if resources.get('com.cloudflare.api.account.zone.*') == '*':
                return True
            for key in ('com.cloudflare.api.account.' + account, 'com.cloudflare.api.account.*'):
                scoped = resources.get(key)
                if isinstance(scoped, dict) and (scoped.get('com.cloudflare.api.account.zone.' + zone) == '*'
                        or scoped.get('com.cloudflare.api.account.zone.*') == '*'):
                    return True
        return False

    def preflight(self):
        """GET-only validation, never use PATCH (even a no-op) as a permission test."""
        self.write_capable = False
        if not self.configured:
            raise ValueError('Cloudflare 凭据不完整；填写 CF_API_TOKEN、CF_ZONE_ID、CF_RECORD_ID。')
        if not all(re.fullmatch('[a-fA-F0-9]{32}', value) for value in (self.zone, self.record)):
            raise ValueError('Cloudflare Zone ID 和 Record ID 必须是有效的 32 位十六进制 ID。')
        try:
            zone = self.api('/zones/' + self.zone)
            current = self.current()
        except Exception:
            raise ValueError('Cloudflare Zone 或 DNS 记录只读校验失败；检查 Token 权限、Zone/Record ID、唯一 DNS-only A 记录及候选 IP。') from None
        if not isinstance(zone, dict) or zone.get('id') != self.zone or zone.get('status') != 'active':
            raise ValueError('Cloudflare Zone 尚未激活或 ID 不匹配；检查域名区域状态。')
        account = zone.get('account', {}).get('id', '')
        prefixes = ['/user/tokens']
        if re.fullmatch('[a-fA-F0-9]{32}', account):
            prefixes.append('/accounts/' + account + '/tokens')
        verified = False
        details = None
        for prefix in prefixes:
            try:
                token = self.api(prefix + '/verify')
                if not isinstance(token, dict) or not re.fullmatch('[a-fA-F0-9]{32}', token.get('id', '')):
                    continue
                if token.get('status') != 'active':
                    continue
                verified = True
                candidate = self.api(prefix + '/' + token['id'])
                if (isinstance(candidate, dict) and candidate.get('id') == token['id'] and
                        candidate.get('status') == 'active'):
                    details = candidate
                    break
            except Exception:
                continue
        if not verified:
            raise ValueError('无法验证 Cloudflare Token 为有效激活状态；检查有效期、来源限制、用户/账户 Token 类型和 API 连通性。')
        if details is None:
            raise ValueError('DNS 写权限未知：无法只读获取当前 Token policy；请核对 Token 的 DNS Edit 范围及读取自身 policy 的权限，或选择模拟模式。不会试写 DNS。')
        if not self.policy_allows_dns_write(details, self.zone, account):
            raise ValueError('DNS 写权限未获证明：Token policy 未明确允许当前 Zone 的 DNS Write，或包含无法安全判断的策略；请检查 Token policy，或选择模拟模式。')
        self.write_capable = True
        return current

    def current(self):
        if not self.configured:
            # Dry run only; the status explicitly distinguishes resolver from API evidence.
            values = set(socket.gethostbyname_ex(DOMAIN)[2])
            if len(values) != 1 or not values.issubset(IPS):
                raise RuntimeError('Unexpected DNS answer')
            return values.pop()
        records = self.api('/zones/' + self.zone + '/dns_records?name=' +
                           urllib.parse.quote(DOMAIN) + '&per_page=100')
        if len(records) != 1:
            raise RuntimeError('Expected exactly one DNS record for this name')
        record = records[0]
        if (record['id'] != self.record or record['type'] != 'A' or
                record['name'] != DOMAIN or record['proxied'] is not False or
                record['content'] not in IPS):
            raise RuntimeError('DNS record identity/type/proxy/content mismatch')
        return record['content']

    def switch(self, expected, target, before_write=None):
        if self.dry_run or not self.write_confirmed:
            raise DNSWriteBlocked('未确认真实模式；DNS 未修改。')
        # Refresh read-only permission evidence immediately before any write.
        try:
            self.preflight()
            if target not in IPS or self.current() != expected:
                raise ValueError('写入前 DNS 已改变或目标不在候选列表；DNS 未修改。')
            if before_write is not None:
                before_write()
        except Exception as error:
            detail = str(error) if isinstance(error, (ValueError, DNSWriteBlocked)) else '写入前只读检查失败；DNS 未修改。'
            raise DNSWriteBlocked(detail) from None
        self.api('/zones/' + self.zone + '/dns_records/' + self.record,
                 {'content': target}, 'PATCH')
        if self.current() != target:
            raise RuntimeError('DNS read-back mismatch')
