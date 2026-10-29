import os
import socket
import urllib.parse
from .common import DOMAIN, IPS, request


class Cloudflare:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.token = os.environ.get('CF_API_TOKEN', '')
        self.zone = os.environ.get('CF_ZONE_ID', '')
        self.record = os.environ.get('CF_RECORD_ID', '')
        self.configured = bool(self.token and self.zone and self.record)
        if not dry_run and not self.configured:
            raise ValueError('Live mode requires scoped Cloudflare credentials and record IDs')

    def api(self, path, payload=None, method=None):
        result = request('https://api.cloudflare.com/client/v4' + path, payload,
                         {'Authorization': 'Bearer ' + self.token}, method)
        if result.get('success') is not True:
            raise RuntimeError('Cloudflare API rejected request')
        return result['result']

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

    def switch(self, expected, target):
        if self.dry_run:
            raise RuntimeError('DNS writes are disabled in dry run')
        if target not in IPS or self.current() != expected:
            raise RuntimeError('DNS changed before write')
        self.api('/zones/' + self.zone + '/dns_records/' + self.record,
                 {'content': target}, 'PATCH')
        if self.current() != target:
            raise RuntimeError('DNS read-back mismatch')
