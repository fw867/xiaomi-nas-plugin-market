"""从宿主机往路由器做 UPnP / NAT-PMP 端口映射（只用标准库）。

为什么不靠 docker 里 Transmission 自带的 UPnP：容器只看得到 Docker 网桥地址
（172.17.0.x），它把这个地址当 internal client 上报给路由器，路由器没法路由过去，
所以映射要么建不起来、要么建了也不通。从宿主机发起就没有这个问题。

对外只有一个 forward_ports()：依次尝试 UPnP（SSDP 发现 + SOAP AddPortMapping）、
再退到 NAT-PMP（RFC 6886），全过程不抛异常，把结果和失败原因放在返回的字典里，
方便插件页直接展示。
"""
from __future__ import annotations

import re
import socket
import struct
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

SSDP_ADDRESS = ('239.255.255.250', 1900)
SSDP_ST = 'urn:schemas-upnp-org:device:InternetGatewayDevice:1'
SSDP_TIMEOUT = 2.5
HTTP_TIMEOUT = 4.0
NATPMP_PORT = 5351
NATPMP_TIMEOUT = 2.5
DEVICE_NS = '{urn:schemas-upnp-org:device-1-0}'
PROC_ROUTE = '/proc/net/route'

NATPMP_REASONS = {
    1: '路由器不支持该操作',
    2: '未授权',
    3: '路由器网络故障',
    4: '资源不足',
    5: '操作码不支持',
}


class UpnpError(RuntimeError):
    pass


def _udp_socket():
    """单独一层是为了可测：直接替换 socket 模块的 socket 会打到别的线程。"""
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def lan_address():
    """本机在局域网的地址（按默认路由探测，避免拿到 docker0 的地址）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('223.5.5.5', 53))
        address = probe.getsockname()[0]
    except OSError:
        return ''
    finally:
        probe.close()
    return address if address and not address.startswith('127.') else ''


def gateway():
    """默认网关地址（NAT-PMP 要用）；从 /proc/net/route 里挑 metric 最小的默认路由。"""
    try:
        with open(PROC_ROUTE, encoding='utf-8') as handle:
            lines = handle.read().splitlines()[1:]
    except OSError:
        return ''
    best = None
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1] != '00000000':
            continue
        try:
            metric = int(fields[6])
        except ValueError:
            metric = 0
        if best is None or metric < best[0]:
            best = (metric, fields[2])
    if not best:
        return ''
    raw = best[1]
    try:
        return '.'.join(str(int(raw[i:i + 2], 16)) for i in (6, 4, 2, 0))
    except ValueError:
        return ''


def ssdp_location(timeout=SSDP_TIMEOUT):
    """SSDP 找 IGD，返回 (描述文件地址, 应答方地址)。"""
    message = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
               'MAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n' % SSDP_ST)
    sock = _udp_socket()
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.sendto(message.encode(), SSDP_ADDRESS)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, address = sock.recvfrom(65507)
            except socket.timeout:
                break
            location = ''
            for line in data.decode('utf-8', 'replace').splitlines():
                if line.lower().startswith('location:'):
                    location = line.split(':', 1)[1].strip()
            if location:
                return location, address[0]
    except OSError as exc:
        raise UpnpError('搜索路由器失败：%s' % exc) from exc
    finally:
        sock.close()
    raise UpnpError('路由器没有响应 UPnP 搜索（可能没开 UPnP）')


def _request(url, data=None, headers=None, timeout=HTTP_TIMEOUT):
    request = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode('utf-8', 'replace')
        except OSError:
            return exc.code, ''
    except (urllib.error.URLError, OSError) as exc:
        raise UpnpError('路由器 %s 无响应：%s' % (url.split('/')[2] if '//' in url else url, exc)) from exc


def control_point(location):
    """读设备描述，返回 (服务类型, 控制地址)。"""
    status, text = _request(location)
    if status != 200:
        raise UpnpError('取路由器描述失败（HTTP %s）' % status)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise UpnpError('路由器描述解析失败：%s' % exc) from exc
    match = re.match(r'(https?://[^/]+)', location)
    base = match.group(1) if match else ''
    for service in root.iter(DEVICE_NS + 'service'):
        service_type = service.findtext(DEVICE_NS + 'serviceType') or ''
        if 'WANIPConnection' not in service_type and 'WANPPPConnection' not in service_type:
            continue
        control = service.findtext(DEVICE_NS + 'controlURL') or ''
        if not control:
            continue
        if not control.startswith('http'):
            control = base + ('/' if not control.startswith('/') else '') + control
        return service_type, control
    raise UpnpError('路由器没有暴露 WAN 连接服务')


def _soap(control, service_type, action, body, timeout=HTTP_TIMEOUT):
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:%s xmlns:u="%s">%s</u:%s>'
        '</s:Body></s:Envelope>' % (action, service_type, body, action))
    status, text = _request(control, data=envelope.encode(), timeout=timeout, headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"%s#%s"' % (service_type, action),
    })
    if status != 200:
        code = _field(text, 'errorCode')
        desc = _field(text, 'errorDescription')
        detail = 'UPnP 错误 %s' % (code or status)
        if desc:
            detail += '（%s）' % desc
        raise UpnpError(detail)
    return text


def _field(text, name):
    match = re.search(r'<%s>([^<]*)</%s>' % (name, name), text)
    return match.group(1) if match else ''


def external_address(control, service_type):
    return _field(_soap(control, service_type, 'GetExternalIPAddress', ''), 'NewExternalIPAddress')


def mapping_status(control, service_type, external_port, protocol):
    """查某个外网端口现有映射；没有映射返回 None。"""
    body = ('<NewRemoteHost></NewRemoteHost><NewExternalPort>%d</NewExternalPort>'
            '<NewProtocol>%s</NewProtocol>' % (external_port, protocol))
    try:
        text = _soap(control, service_type, 'GetSpecificPortMappingEntry', body)
    except UpnpError:
        return None
    return {
        'internal': _field(text, 'NewInternalClient'),
        'internalPort': _field(text, 'NewInternalPort'),
        'description': _field(text, 'NewPortMappingDescription'),
    }


def add_mapping(control, service_type, external_port, internal_port, internal_client,
                protocol, description, lease=0):
    body = ('<NewRemoteHost></NewRemoteHost><NewExternalPort>%d</NewExternalPort>'
            '<NewProtocol>%s</NewProtocol><NewInternalPort>%d</NewInternalPort>'
            '<NewInternalClient>%s</NewInternalClient><NewEnabled>1</NewEnabled>'
            '<NewPortMappingDescription>%s</NewPortMappingDescription>'
            '<NewLeaseDuration>%d</NewLeaseDuration>'
            % (external_port, protocol, internal_port, internal_client, description, lease))
    _soap(control, service_type, 'AddPortMapping', body)
    return mapping_status(control, service_type, external_port, protocol)


def delete_mapping(control, service_type, external_port, protocol):
    """删一条映射。返回 True=确实删掉了；False=本来就没有（errorCode 714）。

    714 不算失败——我们的目的就是"这条映射不存在"。其它错误照旧抛 UpnpError。
    """
    body = ('<NewRemoteHost></NewRemoteHost><NewExternalPort>%d</NewExternalPort>'
            '<NewProtocol>%s</NewProtocol>' % (external_port, protocol))
    try:
        _soap(control, service_type, 'DeletePortMapping', body)
    except UpnpError as exc:
        if '714' in str(exc):
            return False
        raise
    return True


def natpmp_map(gateway_address, internal_port, external_port, protocol='tcp', lifetime=7200):
    """RFC 6886 的 MAP 请求；返回 (是否成功, 说明, 路由器给的租期秒数)。

    NAT-PMP 的映射**不是永久的**（RFC 最多给 7200 秒），到期就没了，
    调用方要在到期前重建，见 Engine.keep_forward_alive。
    """
    opcode = 2 if protocol.lower() == 'tcp' else 1
    packet = struct.pack('!BBHHHI', 0, opcode, 0, internal_port, external_port, lifetime)
    sock = _udp_socket()
    sock.settimeout(NATPMP_TIMEOUT)
    try:
        sock.sendto(packet, (gateway_address, NATPMP_PORT))
        data, _ = sock.recvfrom(64)
    except socket.timeout:
        return False, '路由器没有响应 NAT-PMP', 0
    except OSError as exc:
        return False, 'NAT-PMP 失败：%s' % exc, 0
    finally:
        sock.close()
    if len(data) < 12:
        return False, 'NAT-PMP 应答无效', 0
    result = struct.unpack('!H', data[2:4])[0]
    if result != 0:
        return (False, 'NAT-PMP 被拒绝：%s（result=%d）'
                % (NATPMP_REASONS.get(result, '未知原因'), result), 0)
    mapped = struct.unpack('!H', data[10:12])[0]
    granted = struct.unpack('!I', data[12:16])[0] if len(data) >= 16 else lifetime
    return True, 'NAT-PMP 映射成功（外网端口 %d）' % mapped, granted or lifetime


def _try_upnp(external_port, internal_port, internal_client, description, protocols):
    """走 UPnP 试一遍，返回 (成功协议, 失败说明, 附加信息)。可能抛 UpnpError（找不到路由器）。"""
    location, gateway_address = ssdp_location()
    service_type, control = control_point(location)
    # 记下控制地址：停止服务时要用它把映射删掉，省得再发现一次
    extra = {'gateway': gateway_address, 'service': service_type, 'control': control}
    try:
        extra['external'] = external_address(control, service_type)
    except UpnpError:
        extra['external'] = ''
    mapped, failures = [], []
    for protocol in protocols:
        try:
            add_mapping(control, service_type, external_port, internal_port,
                        internal_client, protocol, description)
            mapped.append(protocol)
        except UpnpError as exc:
            failures.append('%s %s' % (protocol, exc))
    return mapped, failures, extra


def _try_natpmp(gateway_address, internal_port, external_port, protocols):
    """返回 (成功协议, 失败说明, 租期秒数)——取所有协议里最短的租期，保守续期。"""
    mapped, failures, leases = [], [], []
    for protocol in protocols:
        ok, detail, granted = natpmp_map(gateway_address, internal_port, external_port,
                                         protocol=protocol)
        if ok:
            mapped.append(protocol)
            if granted:
                leases.append(granted)
        else:
            failures.append('%s %s' % (protocol, detail))
    return mapped, failures, (min(leases) if leases else 0)


def natpmp_delete(gateway_address, internal_port, external_port, protocol='tcp'):
    """RFC 6886 的删除请求（opcode 3=UDP、4=TCP）；返回 (是否成功, 说明)。"""
    opcode = 4 if protocol.lower() == 'tcp' else 3
    packet = struct.pack('!BBHHH', 0, opcode, 0, internal_port, external_port)
    sock = _udp_socket()
    sock.settimeout(NATPMP_TIMEOUT)
    try:
        sock.sendto(packet, (gateway_address, NATPMP_PORT))
        data, _ = sock.recvfrom(64)
    except socket.timeout:
        return False, '路由器没有响应 NAT-PMP 删除请求'
    except OSError as exc:
        return False, 'NAT-PMP 删除失败：%s' % exc
    finally:
        sock.close()
    if len(data) < 4:
        return False, 'NAT-PMP 应答无效'
    result = struct.unpack('!H', data[2:4])[0]
    if result != 0:
        return (False, 'NAT-PMP 删除被拒绝：%s（result=%d）'
                % (NATPMP_REASONS.get(result, '未知原因'), result))
    return True, 'NAT-PMP 映射已删除'


def _delete_upnp(control, service_type, external_port, protocols):
    """逐个协议删；719/714 那种"本来就没有"也算删掉了（见 delete_mapping）。"""
    deleted = []
    for protocol in protocols:
        delete_mapping(control, service_type, external_port, protocol)
        deleted.append('UPnP ' + protocol)
    return deleted


def remove_forward(external_port, internal_port, method='', gateway_address='',
                   control='', service_type='', protocols=('TCP', 'UDP')):
    """撤掉之前建的映射（停止服务时用），永不抛异常。

    按记录下来的方式删：UPnP 用 DeletePortMapping，NAT-PMP 用 opcode 3/4。
    **存下来的 UPnP 控制地址可能是过期的**——路由器重启会换临时端口（实测
    41795 → 33299），所以失败时会重新发现一次再试。不知道 method 时两条路都试。
    """
    removed, failures = [], []
    wants = [method] if method else ['UPnP', 'NAT-PMP']
    if 'UPnP' in wants:
        points = []
        if control and service_type:
            points.append((control, service_type))
        points.append(None)                       # None = 重新发现兜底
        upnp_failure = ''
        for point in points:
            try:
                if point is None:
                    location, found = ssdp_location()
                    gateway_address = gateway_address or found
                    service_type, control = control_point(location)
                else:
                    control, service_type = point
                removed.extend(_delete_upnp(control, service_type, external_port, protocols))
                upnp_failure = ''
                break
            except UpnpError as exc:
                upnp_failure = 'UPnP：%s' % exc
        if upnp_failure:
            failures.append(upnp_failure)
    if 'NAT-PMP' in wants:
        gateway_address = gateway_address or gateway()
        if gateway_address:
            for protocol in protocols:
                ok, detail = natpmp_delete(gateway_address, internal_port,
                                           external_port, protocol=protocol)
                if ok:
                    removed.append('NAT-PMP ' + protocol)
                else:
                    failures.append('%s %s' % (protocol, detail))
        else:
            failures.append('NAT-PMP：找不到默认网关')
    removed = list(dict.fromkeys(removed))
    if removed and not failures:
        return {'ok': True, 'detail': '已移除路由器映射（%s）' % '、'.join(removed)}
    if removed:
        return {'ok': True, 'detail': '部分移除：%s；%s' % ('、'.join(removed), '；'.join(failures))}
    return {'ok': False, 'detail': '；'.join(failures) or '没有可移除的映射'}


def forward_ports(external_port, internal_port, internal_client, description,
                  protocols=('TCP', 'UDP')):
    """尝试把 external_port 映射到 internal_client:internal_port，永不抛异常。

    先 UPnP（大多数路由器都认），不行再 NAT-PMP；两者都失败时 detail 里写清
    卡在哪一步，页面直接展示。成功协议与失败协议都记在返回值里——TCP 通了、
    UDP 没通也算部分成功（BT 至少能连上 TCP）。"""
    state = {
        'ok': False,
        'method': '',
        'detail': '',
        'gateway': '',
        'external': '',
        'internal': internal_client,
        'internalPort': internal_port,
        'externalPort': external_port,
        'protocols': list(protocols),
        'mapped': [],
        # 映射租期秒数：UPnP 用 lease=0（永久）所以是 0；NAT-PMP 由路由器给定，
        # 非 0 就要在到期前重建
        'lease': 0,
        'at': int(time.time()),
    }
    problems = []
    gateway_address = ''
    try:
        mapped, failures, extra = _try_upnp(external_port, internal_port, internal_client,
                                            description, protocols)
        gateway_address = extra.get('gateway') or ''
        state['gateway'] = gateway_address
        state['external'] = extra.get('external') or ''
        state['control'] = extra.get('control') or ''
        state['service'] = extra.get('service') or ''
        if mapped:
            state['mapped'] = mapped
            state['method'] = 'UPnP'
            if not failures:
                state.update(ok=True, detail='UPnP 映射成功（%d → %s:%d）'
                                             % (external_port, internal_client, internal_port))
                return state
            state['detail'] = 'UPnP 部分成功（%s），%s' % ('/'.join(mapped), '；'.join(failures))
            return state
        problems.extend('UPnP：%s' % item for item in failures)
    except UpnpError as exc:
        problems.append('UPnP：%s' % exc)
    gateway_address = gateway_address or gateway()
    state['gateway'] = state['gateway'] or gateway_address
    if gateway_address:
        mapped, failures, lease = _try_natpmp(gateway_address, internal_port,
                                              external_port, protocols)
        if mapped:
            state['mapped'] = mapped
            state['method'] = 'NAT-PMP'
            state['lease'] = lease
            if not failures:
                state.update(ok=True, detail='NAT-PMP 映射成功（%d → %s:%d，租期 %d 秒）'
                                             % (external_port, internal_client, internal_port,
                                                lease))
                return state
            state['detail'] = 'NAT-PMP 部分成功（%s），%s' % ('/'.join(mapped), '；'.join(failures))
            return state
        problems.extend('NAT-PMP：%s' % item for item in failures)
    state['detail'] = '；'.join(problems) or '没有找到可用的 UPnP/NAT-PMP 路由器'
    return state

