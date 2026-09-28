#!/usr/bin/env python3
"""WSD（WS-Discovery）回应器：让小米存储在 Windows 的「网络」里出现。

为什么要自己写（2026-09-28 在 NAS 上做的字节级对比实测）：

- 官方 `/usr/bin/wsdd` 发出的 WSD 消息**没有 `wsd:AppSequence` 头**。WS-Discovery
  规范要求 Hello / Bye / ProbeMatches / ResolveMatches 都必须带它，Windows 严格照此
  执行：缺了 AppSequence 的消息会被**整条丢掉**，Windows 连设备描述都不会来取。
  自研的第一版同样漏了这一项，症状与官方完全一样（Probe 收得到、回过去没人理）。
- 补上之后，Windows 会立刻来取描述——而那一步是 `WS-Transfer Get`（SOAP **POST**）
  打到 XAddrs 上。官方 wsdd 对这个 POST 直接断开连接
  （`curl: (52) Empty reply from server`），而同一个地址用普通 HTTP GET 却返回 200。
- 描述响应还必须带 `wsa:RelatesTo`（WS-Addressing 关联请求的 MessageID）：少了它
  Windows 会丢弃整份描述，表现为“来取了元数据，但主机还是不出现在网络里”。

三条都满足之后，「网络」里就会出现 SMARTSTORAGE（本机 Windows 实测），可以直接双击
进去浏览 SMB 共享；同网段的 Windows 主机、路由器本来就在，唯独小米存储不出现——
这正是用户的原始症状。

本模块实现完整的服务端一侧：

- **组播监听 3702**：`Probe` → `ProbeMatches`，`Resolve` → `ResolveMatches`；
- **主动通告**：启动 `Hello`、退出 `Bye`、可选的周期 `Hello`；
- **元数据 HTTP 服务**：普通 GET 与 `WS-Transfer Get`（POST）都返回 `wsx:Metadata`，
  并按请求回填 `wsa:RelatesTo`；
- **身份落盘**：`urn:uuid` 存在 state 文件里复用，避免每次重启在 Windows 里
  变成一台新设备。

注意：响应必须从 **3702** 发出；组播出口要钉在局域网网卡上（否则可能从 docker0 出去）。

只用标准库，可单独运行（也便于在没有插件环境时排查）：

    python3 wsd.py --hostname SmartStorage --workgroup WORKGROUP --state /tmp/wsd.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WSD_GROUP = '239.255.255.250'
WSD_PORT = 3702
DISCOVERY = 'urn:schemas-xmlsoap-org:ws:2005:04:discovery'
ANONYMOUS = 'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'

HELLO_ACTION = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/Hello'
BYE_ACTION = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/Bye'
PROBE_ACTION = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe'
PROBE_MATCHES_ACTION = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches'
RESOLVE_ACTION = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/Resolve'
RESOLVE_MATCHES_ACTION = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/ResolveMatches'
TRANSFER_GET_ACTION = 'http://schemas.xmlsoap.org/ws/2004/09/transfer/Get'

# 设备类型：Windows 认 pub:Computer 才会在「网络」里当计算机列出来
DEVICE_TYPES = 'wsdp:Device pub:Computer'
MODEL_NAME = 'Xiaomi Smart Storage'
MANUFACTURER = 'Xiaomi'


def envelope(header: str, body: str) -> str:
    """套上 SOAP 信封与全部命名空间（与官方 wsdd 的输出保持一致）。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope'
        ' xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:wsx="http://schemas.xmlsoap.org/ws/2004/09/mex"'
        ' xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof"'
        ' xmlns:pnpx="http://schemas.microsoft.com/windows/pnpx/2005/10"'
        ' xmlns:pub="http://schemas.microsoft.com/windows/pub/2005/07">'
        '<soap:Header>%s</soap:Header><soap:Body>%s</soap:Body></soap:Envelope>'
    ) % (header, body)


def endpoint_reference(identity: str) -> str:
    return ('<wsa:EndpointReference><wsa:Address>urn:uuid:%s</wsa:Address>'
            '</wsa:EndpointReference>' % identity)


def _new_message_id() -> str:
    return 'urn:uuid:%s' % uuid.uuid4()


class AppSequence:
    """WS-Discovery 的 `wsd:AppSequence` 头。

    **这一项不能省**：规范要求 Hello / Bye / ProbeMatches / ResolveMatches 都必须带它，
    Windows 也照此执行——缺了 AppSequence 的消息会被整条丢掉（连元数据都不会来取）。
    2026-09-28 在 NAS 上对比上游 wsdd 与自研实现时抓到的唯一实质差异就是它：
    官方 wsdd 同样没带，所以它的 ProbeMatch 也一直不被理睬。

    InstanceId 用进程启动时间，SequenceId 每次启动换一个，MessageNumber 递增。
    """

    def __init__(self):
        self.instance_id = int(time.time())
        self.sequence_id = str(uuid.uuid4())
        self.counter = 0

    def element(self) -> str:
        self.counter += 1
        return ('<wsd:AppSequence InstanceId="%d" SequenceId="urn:uuid:%s"'
                ' MessageNumber="%d" />'
                % (self.instance_id, self.sequence_id, self.counter))


def hello_message(identity: str, xaddrs: str, app_sequence: str = '') -> str:
    """Hello：向网内宣告「我来了」。

    注意两点（都是 NAS 实测踩出来的）：
    - **不要**在 Hello 里塞 `wsd:Types`（上游 wsdd 也不塞）；
    - **一定要**带 AppSequence，否则 Windows 直接忽略。
    """
    header = ('<wsa:To>%s</wsa:To><wsa:Action>%s</wsa:Action>'
              '<wsa:MessageID>%s</wsa:MessageID>%s'
              % (DISCOVERY, HELLO_ACTION, _new_message_id(), app_sequence))
    body = ('<wsd:Hello>%s<wsd:XAddrs>%s</wsd:XAddrs>'
            '<wsd:MetadataVersion>1</wsd:MetadataVersion></wsd:Hello>'
            % (endpoint_reference(identity), xaddrs))
    return envelope(header, body)


def bye_message(identity: str, app_sequence: str = '') -> str:
    """Bye：插件停用/卸载时告诉 Windows「我走了」，别留下死图标。"""
    header = ('<wsa:To>%s</wsa:To><wsa:Action>%s</wsa:Action>'
              '<wsa:MessageID>%s</wsa:MessageID>%s'
              % (DISCOVERY, BYE_ACTION, _new_message_id(), app_sequence))
    body = '<wsd:Bye>%s</wsd:Bye>' % endpoint_reference(identity)
    return envelope(header, body)


def _match_body(identity: str, xaddrs: str, element: str) -> str:
    plural = element + 'es'
    return ('<wsd:%s><wsd:%s>%s<wsd:Types>%s</wsd:Types><wsd:XAddrs>%s</wsd:XAddrs>'
            '<wsd:MetadataVersion>1</wsd:MetadataVersion></wsd:%s></wsd:%s>'
            % (plural, element, endpoint_reference(identity), DEVICE_TYPES, xaddrs,
               element, plural))


def probe_matches_message(identity: str, xaddrs: str, relates_to: str,
                          reply_to: str = '', app_sequence: str = '') -> str:
    header = ('<wsa:To>%s</wsa:To><wsa:Action>%s</wsa:Action>'
              '<wsa:MessageID>%s</wsa:MessageID><wsa:RelatesTo>%s</wsa:RelatesTo>%s'
              % (reply_to or ANONYMOUS, PROBE_MATCHES_ACTION,
                 _new_message_id(), relates_to, app_sequence))
    return envelope(header, _match_body(identity, xaddrs, 'ProbeMatch'))


def resolve_matches_message(identity: str, xaddrs: str, relates_to: str,
                            reply_to: str = '', app_sequence: str = '') -> str:
    header = ('<wsa:To>%s</wsa:To><wsa:Action>%s</wsa:Action>'
              '<wsa:MessageID>%s</wsa:MessageID><wsa:RelatesTo>%s</wsa:RelatesTo>%s'
              % (reply_to or ANONYMOUS, RESOLVE_MATCHES_ACTION,
                 _new_message_id(), relates_to, app_sequence))
    return envelope(header, _match_body(identity, xaddrs, 'ResolveMatch'))


def metadata_message(identity: str, hostname: str, workgroup: str,
                     presentation: str = '', relates_to: str = '') -> str:
    """设备描述：字段与上游 wsdd 完全一致（那是 Windows 认的形状）。

    `pub:Computer` 里的 `主机名/Workgroup:工作组` 是资源管理器显示的凭据，
    主机名与工作组都**大写**（上游默认如此，资源管理器里也就显示成 SMARTSTORAGE）。
    `relates_to` 是请求的 MessageID：WS-Addressing 要求响应关联请求，
    少了这一项 Windows 会丢弃整份描述——实测就是这一条让它始终不列主机。
    """
    name = (hostname or 'SmartStorage').upper()
    group = (workgroup or 'WORKGROUP').upper()
    header = ('<wsa:To>%s</wsa:To><wsa:Action>'
              'http://schemas.xmlsoap.org/ws/2004/09/transfer/GetResponse</wsa:Action>'
              '<wsa:MessageID>%s</wsa:MessageID>%s'
              % (ANONYMOUS, _new_message_id(),
                 ('<wsa:RelatesTo>%s</wsa:RelatesTo>' % relates_to) if relates_to else ''))
    this_device = (
        '<wsx:MetadataSection Dialect='
        '"http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisDevice">'
        '<wsdp:ThisDevice><wsdp:FriendlyName>WSD Device %s</wsdp:FriendlyName>'
        '<wsdp:FirmwareVersion>1.0</wsdp:FirmwareVersion>'
        '<wsdp:SerialNumber>1</wsdp:SerialNumber></wsdp:ThisDevice>'
        '</wsx:MetadataSection>' % hostname)
    this_model = (
        '<wsx:MetadataSection Dialect='
        '"http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisModel">'
        '<wsdp:ThisModel><wsdp:Manufacturer>%s</wsdp:Manufacturer>'
        '<wsdp:ModelName>%s</wsdp:ModelName>'
        '<pnpx:DeviceCategory>Computers</pnpx:DeviceCategory>'
        '</wsdp:ThisModel></wsx:MetadataSection>' % (MANUFACTURER, MODEL_NAME))
    relationship = (
        '<wsx:MetadataSection Dialect='
        '"http://schemas.xmlsoap.org/ws/2006/02/devprof/Relationship">'
        '<wsdp:Relationship Type='
        '"http://schemas.xmlsoap.org/ws/2006/02/devprof/host"><wsdp:Host>%s'
        '<wsdp:Types>pub:Computer</wsdp:Types>'
        '<wsdp:ServiceId>urn:uuid:%s</wsdp:ServiceId>'
        '<pub:Computer>%s/Workgroup:%s</pub:Computer>'
        '</wsdp:Host></wsdp:Relationship></wsx:MetadataSection>'
        % (endpoint_reference(identity), identity, name, group))
    body = ('<wsx:Metadata>%s%s%s</wsx:Metadata>'
            % (this_device, this_model, relationship))
    return envelope(header, body)


def field(text: str, name: str) -> str:
    """从 SOAP 文本里抓一个元素的内容（消息都是我们自己造的，正则够用）。"""
    match = re.search(r'<%s>(.*?)</%s>' % (name, name), text, re.S)
    return match.group(1).strip() if match else ''


def wants_us(types: str) -> bool:
    """Probe 里的 Types 是否包含我们；空的 Types 表示「谁都可以答」。"""
    if not types:
        return True
    wanted = types.split()
    return any(item in wanted for item in DEVICE_TYPES.split())


def lan_address(fallback: str = '') -> str:
    """按默认路由探测本机局域网地址（不会错拿 docker0）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('223.5.5.5', 53))
        address = probe.getsockname()[0]
        return address if address and not address.startswith('127.') else fallback
    except OSError:
        return fallback
    finally:
        probe.close()


def load_identity(state_file: str) -> str:
    """身份落盘复用：每次重启换 UUID 会让 Windows 里多出一台幽灵设备。"""
    if state_file:
        try:
            with open(state_file, encoding='utf-8') as handle:
                saved = json.load(handle)
            identity = str(saved.get('identity', ''))
            if re.fullmatch(r'[0-9a-fA-F-]{36}', identity):
                return identity.lower()
        except (OSError, ValueError):
            pass
    identity = str(uuid.uuid4())
    if state_file:
        try:
            directory = os.path.dirname(state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(state_file, 'w', encoding='utf-8') as handle:
                json.dump({'identity': identity}, handle)
        except OSError:
            pass
    return identity


class MetadataHandler(BaseHTTPRequestHandler):
    """XAddrs 指向的元数据服务；GET 与 WS-Transfer Get 都算数。"""

    protocol_version = 'HTTP/1.1'
    server_version = 'netneighbor-wsd'

    def do_GET(self):                                            # noqa: N802
        self.log_request_line('GET', '')
        self.send_metadata()

    def do_POST(self):                                           # noqa: N802
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b''
        action = ''
        match = re.search(rb'<wsa:Action>(.*?)</wsa:Action>', body, re.S)
        if match:
            action = match.group(1).decode('utf-8', 'replace').strip()
        self.log_request_line('POST', action)
        if action in (TRANSFER_GET_ACTION, ''):
            relates = ''
            found = re.search(rb'<wsa:MessageID>(.*?)</wsa:MessageID>', body, re.S)
            if found:
                relates = found.group(1).decode('utf-8', 'replace').strip()
            self.send_metadata(relates)
        else:
            self.send_payload(500, envelope(
                '<wsa:Action>http://schemas.xmlsoap.org/ws/2004/09/transfer/GetResponse'
                '</wsa:Action>',
                '<soap:Fault><soap:Code><soap:Value>soap:Sender</soap:Value>'
                '</soap:Code><soap:Reason><soap:Text>不支持的请求</soap:Text>'
                '</soap:Reason></soap:Fault>'))

    def log_request_line(self, method: str, action: str):
        """Windows 到底有没有来取元数据，全靠这行日志判断。"""
        if self.server.on_log:
            self.server.on_log('wsd http: %s %s from %s%s'
                               % (method, self.path, self.client_address[0],
                                  (' action=%s' % action.rsplit('/', 1)[-1]) if action else ''))

    def send_metadata(self, relates_to: str = ''):
        render = getattr(self.server, 'render_metadata', None)
        self.send_payload(200, render(relates_to) if render else self.server.metadata)

    def send_payload(self, status: int, text: str):
        payload = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/soap+xml; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):                          # noqa: A003
        """交给插件自己的日志，别往 stderr 刷。"""
        if self.server.on_log:
            self.server.on_log('wsd http: ' + (fmt % args))


class WsdResponder:
    """一个进程里跑的一整套 WSD 服务端。"""

    def __init__(self, hostname='SmartStorage', workgroup='WORKGROUP',
                 address='', port=5357, state_file='', hello_interval=900,
                 on_log=None):
        self.hostname = hostname or 'SmartStorage'
        self.workgroup = workgroup or 'WORKGROUP'
        self.address = address
        self.port = int(port)
        self.state_file = state_file
        self.hello_interval = max(0, int(hello_interval))
        self.on_log = on_log or (lambda message: None)
        self.identity = ''
        self.sequence = AppSequence()
        self.metadata = ''
        self.http = None
        self.udp = None
        self.send_socket = None
        self.threads = []
        self.stopping = threading.Event()

    # ---- 对外属性 ----
    @property
    def xaddrs(self) -> str:
        return 'http://%s:%d/%s' % (self.address, self.port, self.identity)

    def log(self, message: str):
        self.on_log(message)

    # ---- 生命周期 ----
    def start(self):
        self.address = self.address or lan_address()
        if not self.address:
            raise RuntimeError('拿不到局域网地址，无法启动 WSD 回应器')
        self.identity = load_identity(self.state_file)
        self.metadata = metadata_message(
            self.identity, self.hostname, self.workgroup,
            'http://%s' % self.address)

        try:
            # 先绑 UDP 3702：它是最容易冲突的资源，放在最前面。
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp.bind(('0.0.0.0', WSD_PORT))
            self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                struct.pack('4s4s', socket.inet_aton(WSD_GROUP),
                                            socket.inet_aton(self.address)))
            self.udp.settimeout(1)
            # 回应必须**从 3702 端口发出**（WSD 规范如此，Windows 只认这种回应；
            # 用临时端口发出去的 ProbeMatches 会被直接丢掉——NAS 上实测踩过）。
            self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            # 组播出口也要钉在局域网网卡上，否则可能从 docker0 之类的接口出去。
            self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(self.address))
            self.send_socket = self.udp
            self._spawn(self._listen, 'wsd-udp')

            self.http = ThreadingHTTPServer(('0.0.0.0', self.port), MetadataHandler)
            self.http.daemon_threads = True
            self.http.metadata = self.metadata
            self.http.render_metadata = lambda relates: metadata_message(
                self.identity, self.hostname, self.workgroup, relates_to=relates)
            self.http.on_log = self.log
            self._spawn(self.http.serve_forever, 'wsd-http')

            self.announce_hello()
            if self.hello_interval:
                self._spawn(self._hello_loop, 'wsd-hello')
        except Exception:
            # 半启动状态必须收拾干净：否则元数据 HTTP 端口会被自己占住，
            # 之后每次重试都是 EADDRINUSE（NAS 上实测踩过这个坑）。
            self.stop()
            raise
        self.log('WSD 回应器已启动：%s（%s / %s）'
                 % (self.xaddrs, self.hostname, self.workgroup))
        return self

    def stop(self):
        self.stopping.set()
        if self.send_socket and self.identity:
            try:
                self.send_socket.sendto(
                    bye_message(self.identity, self.sequence.element()).encode(),
                    (WSD_GROUP, WSD_PORT))
            except OSError:
                pass
        for sock in (self.udp, self.send_socket):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.http:
            self.http.shutdown()
            self.http.server_close()
        self.log('WSD 回应器已停止')

    def _spawn(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    # ---- 收发 ----
    def announce_hello(self, times: int = 2, gap: float = 0.3):
        """Hello 发两遍：UDP 组播会丢，两遍几乎不增加开销。"""
        for index in range(max(1, times)):
            try:
                self.send_socket.sendto(
                    hello_message(self.identity, self.xaddrs,
                                  self.sequence.element()).encode(),
                    (WSD_GROUP, WSD_PORT))
            except OSError as exc:
                self.log('发送 Hello 失败：%s' % exc)
                return False
            if index + 1 < times:
                time.sleep(gap)
        return True

    def _hello_loop(self):
        while not self.stopping.wait(self.hello_interval):
            self.announce_hello()

    def _listen(self):
        while not self.stopping.is_set():
            try:
                data, sender = self.udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(data.decode('utf-8', 'replace'), sender)
            except Exception as exc:                            # noqa: BLE001
                self.log('处理 WSD 消息出错：%s' % exc)

    def _handle(self, text: str, sender):
        action = field(text, 'wsa:Action')
        message_id = field(text, 'wsa:MessageID')
        self.log('收到 %s from %s:%d' % (action.rsplit('/', 1)[-1] or '未知',
                                        sender[0], sender[1]))
        reply_to = re.search(r'<wsa:ReplyTo>\s*<wsa:Address>(.*?)</wsa:Address>',
                             text, re.S)
        reply_to = reply_to.group(1).strip() if reply_to else ''
        if action == PROBE_ACTION:
            if not wants_us(field(text, 'wsd:Types')):
                return
            payload = probe_matches_message(self.identity, self.xaddrs,
                                            message_id, reply_to,
                                            self.sequence.element())
        elif action == RESOLVE_ACTION:
            target = re.search(r'<wsa:Address>(urn:uuid:[^<]+)</wsa:Address>', text)
            if target and target.group(1).strip().lower() != 'urn:uuid:%s' % self.identity:
                return
            payload = resolve_matches_message(self.identity, self.xaddrs,
                                              message_id, reply_to,
                                              self.sequence.element())
        else:
            return
        self.send_socket.sendto(payload.encode(), sender)


def main():
    parser = argparse.ArgumentParser(description='WSD 回应器（让 Windows 网络邻居看到本机）')
    parser.add_argument('--hostname', default=socket.gethostname() or 'SmartStorage')
    parser.add_argument('--workgroup', default='WORKGROUP')
    parser.add_argument('--address', default='', help='局域网地址，默认按默认路由探测')
    parser.add_argument('--port', type=int, default=5357, help='元数据 HTTP 端口')
    parser.add_argument('--state', default='', help='身份落盘文件')
    parser.add_argument('--hello-interval', type=int, default=900)
    parser.add_argument('--once', action='store_true', help='只发一次 Hello 就退出（自检用）')
    args = parser.parse_args()

    responder = WsdResponder(hostname=args.hostname, workgroup=args.workgroup,
                             address=args.address, port=args.port,
                             state_file=args.state,
                             hello_interval=args.hello_interval,
                             on_log=lambda message: print(message, flush=True))
    if args.once:
        responder.address = responder.address or lan_address()
        responder.identity = load_identity(responder.state_file)
        responder.send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        responder.send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                        socket.inet_aton(responder.address))
        print('Hello 已发送：%s -> %s' % (responder.xaddrs, WSD_GROUP))
        responder.announce_hello(1)
        return
    responder.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        responder.stop()


if __name__ == '__main__':
    main()
