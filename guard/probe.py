"""Local, source-bound IPv4 TCP probes. No proxy environment is consulted."""
import concurrent.futures
import errno
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import statistics
import time
from .common import IPS, PORT, MONITOR_ID, PROBE_TIMEOUT, HEALTHY_RATIO, INTERVAL, EGRESS_PRIVATE_IP

DEFAULT_TARGETS = [
    {'name':'beijing', 'host':'oss-cn-beijing.aliyuncs.com', 'port':443},
    {'name':'hangzhou', 'host':'oss-cn-hangzhou.aliyuncs.com', 'port':443},
    {'name':'shenzhen', 'host':'oss-cn-shenzhen.aliyuncs.com', 'port':443},
]
ECHOES = [('api.ipify.org','/'), ('www.cloudflare.com','/cdn-cgi/trace')]


def source_map(value, ips=IPS):
    sources = [str(ipaddress.IPv4Address(x.strip())) for x in value.split(',')]
    if len(sources) != len(ips) or len(set(sources)) != len(sources) or len(ips) > 16:
        raise ValueError('SOURCE_IPS must match ENTRY_IPS one-to-one (maximum 16)')
    if any(ipaddress.ip_address(x).is_unspecified or ipaddress.ip_address(x).is_loopback for x in sources):
        raise ValueError('SOURCE_IPS must be assigned local interface addresses')
    if EGRESS_PRIVATE_IP and EGRESS_PRIVATE_IP in sources:
        raise ValueError('Fixed egress private IP cannot be used for entry probes')
    return dict(zip(ips,sources))


def targets_config(value):
    targets = json.loads(value)
    if not isinstance(targets,list) or not 3 <= len(targets) <= 12:
        raise ValueError('PROBE_TARGETS requires 3 to 12 distinct regional TCP targets')
    names, endpoints = set(), set()
    for target in targets:
        if not isinstance(target,dict) or set(target) != {'name','host','port'}:
            raise ValueError('Each target requires name, host and port')
        if not isinstance(target['name'],str) or not re.fullmatch('[a-zA-Z0-9_-]{1,40}',target['name']):
            raise ValueError('Target name must be a short alphanumeric label')
        if not isinstance(target['host'],str) or not re.fullmatch('[a-zA-Z0-9.-]{1,253}',target['host']):
            raise ValueError('Invalid probe hostname')
        if type(target['port']) is not int or not 1 <= target['port'] <= 65535:
            raise ValueError('Invalid probe port')
        if target['name'] in names or (target['host'],target['port']) in endpoints:
            raise ValueError('Duplicate target name or endpoint')
        names.add(target['name']); endpoints.add((target['host'],target['port']))
    return targets


def resolve(host):
    address = socket.getaddrinfo(host,None,socket.AF_INET,socket.SOCK_STREAM)[0][4][0]
    if not ipaddress.IPv4Address(address).is_global:
        raise ValueError('Probe endpoints must resolve to public IPv4 addresses')
    return address


def tcp(source, address, port):
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            sock.settimeout(PROBE_TIMEOUT)
            if source:
                sock.bind((source,0))
            sock.connect((address,port))
        return {'up':True,'latency_ms':round((time.monotonic()-started)*1000,3)}
    except OSError as error:
        # Missing local addresses/permissions are system errors, not target failures.
        if error.errno in (errno.EADDRNOTAVAIL,errno.EACCES,errno.EPERM,errno.EMFILE,errno.ENFILE):
            raise
        return {'up':False,'latency_ms':None}


def public_ip(source, endpoint):
    host, path = endpoint
    address = resolve(host)
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
        sock.settimeout(PROBE_TIMEOUT)
        sock.bind((source,0))
        sock.connect((address,443))
        with ssl.create_default_context().wrap_socket(sock,server_hostname=host) as tls:
            connection = http.client.HTTPConnection(host,timeout=PROBE_TIMEOUT)
            connection.sock = tls
            connection.request('GET',path,headers={'Host':host,'Connection':'close'})
            response = connection.getresponse()
            body = response.read(4096).decode().strip()
            if response.status != 200:
                raise ValueError('Public source verification rejected')
    observed = body if host == 'api.ipify.org' else next((x[3:] for x in body.splitlines() if x.startswith('ip=')), '')
    return str(ipaddress.IPv4Address(observed))


class Prober:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.sources = source_map(os.environ.get('SOURCE_IPS',''))
        self.targets = targets_config(os.environ.get('PROBE_TARGETS',json.dumps(DEFAULT_TARGETS)))
        if ((len(self.sources)*len(self.targets)+7)//8)*PROBE_TIMEOUT > INTERVAL*0.6:
            raise ValueError('Too many targets for probe interval/timeout; increase interval')
        self.mapping_at = 0
        self.mapping = {}
        self.mapping_status = {}

    def verify_mapping(self):
        if self.mapping and 0 <= self.clock()-self.mapping_at < 300:
            return all(self.mapping.values())
        def check(item):
            ip, source = item
            statuses = []
            for endpoint in ECHOES:
                try:
                    statuses.append('verified' if public_ip(source,endpoint) == ip else 'mismatch')
                except (TimeoutError, ConnectionError):
                    statuses.append('unavailable')
                except OSError as error:
                    statuses.append('unavailable' if error.errno in (
                        errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.EHOSTUNREACH,
                        errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ECONNRESET) else 'unknown')
                except Exception:
                    statuses.append('unknown')
            status = ('mismatch' if 'mismatch' in statuses else
                      'unknown' if 'unknown' in statuses else
                      'unavailable' if 'unavailable' in statuses else 'verified')
            return ip, status
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.mapping_status = dict(pool.map(check,self.sources.items()))
        self.mapping = {ip: status == 'verified' for ip,status in self.mapping_status.items()}
        self.mapping_at = self.clock()
        # Failed verification is retried next cycle, not cached for five minutes.
        if not all(self.mapping.values()):
            self.mapping_at = 0
        return (any(self.mapping.values()) and
                not any(v in ('mismatch','unknown') for v in self.mapping_status.values()))

    def cycle(self):
        started = self.clock()
        mapping_ok = self.verify_mapping()
        resolved = [(t,resolve(t['host'])) for t in self.targets]
        # Each configured region has one vote; DNS errors invalidate the whole cycle.
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            controls = list(pool.map(lambda ip:tcp(None,ip,443),['1.1.1.1','8.8.8.8']))
            jobs = [(ip,source,t,addr) for ip,source in self.sources.items()
                    if self.mapping.get(ip, False) for t,addr in resolved]
            def probe(job):
                ip,source,t,address = job
                return ip,dict(name=t['name'],address=address,port=t['port'],**tcp(source,address,t['port']))
            rows = list(pool.map(probe,jobs)) if mapping_ok else []
        samples = {}
        for ip in self.sources:
            regions = [row for candidate,row in rows if candidate == ip]
            latencies = [r['latency_ms'] for r in regions if r['up']]
            ratio = len(latencies)/len(self.targets)
            samples[ip] = {'up':ratio>=HEALTHY_RATIO, 'port':PORT,
                'latency_ms':statistics.median(latencies) if latencies else None,
                'success_ratio':ratio, 'regions':regions,
                'mapping_status':self.mapping_status.get(ip,'unknown')}
        return {'agent':MONITOR_ID,'observed_at':started,'direct_ok':mapping_ok,
                'internet_ok':any(r['up'] for r in controls),'samples':samples}


if __name__ == '__main__':
    print(json.dumps(Prober().cycle(),indent=2))
