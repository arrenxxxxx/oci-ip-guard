import hashlib
import hmac
import ipaddress
import json
import os
import urllib.request

def integer(name, default, minimum=1, maximum=86400):
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(name + ' outside allowed range')
    return value


# Documentation-only addresses make the example safe to distribute.
IPS = [str(ipaddress.IPv4Address(ip.strip())) for ip in os.environ.get(
    'ENTRY_IPS', '192.0.2.10,198.51.100.20,203.0.113.30').split(',')]
if len(IPS) < 2 or len(set(IPS)) != len(IPS):
    raise ValueError('ENTRY_IPS requires at least two unique IPv4 addresses')
DOMAIN = os.environ.get('DNS_NAME', 'edge.example.com').strip().lower().rstrip('.')
if not DOMAIN or '/' in DOMAIN or any(c.isspace() for c in DOMAIN):
    raise ValueError('Invalid DNS_NAME')
PORT = integer('PROBE_PORT', 443, maximum=65535)
MONITOR_ID = os.environ.get('MONITOR_ID', 'monitor-main')
INTERVAL = integer('PROBE_INTERVAL_SECONDS', 60, minimum=20, maximum=3600)
FRESH = integer('STALE_AFTER_SECONDS', 150)
MAX_SAMPLE_AGE = integer('MAX_SAMPLE_AGE_SECONDS', 90)
FAILURE_THRESHOLD = integer('FAILURE_THRESHOLD', 3, maximum=100)
HEALTHY_THRESHOLD = integer('HEALTHY_THRESHOLD', 3, maximum=100)
COOLDOWN = integer('COOLDOWN_SECONDS', 900)
PROBE_TIMEOUT = integer('PROBE_TIMEOUT_SECONDS', 4, maximum=30)
if not PROBE_TIMEOUT < INTERVAL or not MAX_SAMPLE_AGE <= FRESH or FRESH <= INTERVAL:
    raise ValueError('Require probe timeout < interval < stale limit and max age <= stale limit')


def signature(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def request(url, payload=None, headers=None, method=None):
    # Never inherit HTTP(S)_PROXY, and never forward credentials on redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    with opener.open(req, timeout=8) as response:
        return json.load(response)


def secret(name):
    value = os.environ.get(name, '')
    if len(value) < 32 or value.startswith('replace-'):
        raise ValueError(name + ' must contain at least 32 characters')
    return value

FAILURE_RATIO = float(os.environ.get('FAILURE_RATIO', '0.5'))
HEALTHY_RATIO = float(os.environ.get('HEALTHY_RATIO', '0.8'))
if not 0 < FAILURE_RATIO < HEALTHY_RATIO <= 1:
    raise ValueError('Require 0 < FAILURE_RATIO < HEALTHY_RATIO <= 1')
