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
        self.record_ready = False
        self.preflight_error = ''

    def api(self, path, payload=None, method=None):
        result = request('https://api.cloudflare.com/client/v4' + path, payload,
                         {'Authorization': 'Bearer ' + self.token}, method)
        if result.get('success') is not True:
            raise RuntimeError('Cloudflare API rejected request')
        return result['result']

    def preflight(self):
        """Validate the DNS record only; never inspect Token policies or test writes."""
        self.record_ready = False
        if not self.configured:
            raise ValueError('Cloudflare 凭据不完整；填写 CF_API_TOKEN、CF_ZONE_ID、CF_RECORD_ID。')
        if not all(re.fullmatch('[a-fA-F0-9]{32}', value) for value in (self.zone, self.record)):
            raise ValueError('Cloudflare Zone ID 和 Record ID 必须是有效的 32 位十六进制 ID。')
        try:
            current = self.current()
        except Exception:
            raise ValueError('DNS 记录读取/身份校验失败；检查凭据、唯一 DNS-only A 记录及候选 IP。') from None
        self.record_ready = True
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
        # Check the current record immediately before writing; PATCH decides write access.
        try:
            current = self.preflight()
            if target not in IPS or current != expected:
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
