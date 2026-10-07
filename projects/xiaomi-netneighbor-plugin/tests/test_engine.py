from __future__ import annotations

import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import wsd
from engine import Engine, Error, Result

# 真机（NAS 192.168.1.8）实测的 UCI 形状：
#   config sambauser 'u3943892_1'   ← 段 id = <NAS用户>_<序号>
#       option user 'u3943892'      ← NAS 用户号
#       option name 'fw867'         ← 账号名（SMB 用户是 samba<账号>）
#       list dirs '/home/u3943892/pool0/data/下载'   ← **目录路径**，不是共享段 id
#   config sambashare 'u3943892_nb_1'   ← 段 id 才是共享标识（add_dir/del_dir 用它）
#       option name '照片-3943892'      ← 资源管理器里显示的共享名（smb.conf 段名）
SAMBASHARE_TEXT = """config sambashare 'u3943892_nb_1'
\toption name '照片-3943892'
\toption path '/home/u3943892/pool0/data/我的照片'
\tlist users 'sambafw867'
\toption user_force 'u3943892'
\toption status '1'
\toption comment '我的照片'

config sambashare 'public'
\toption name 'public'
\toption path '/nas/pool0/public'
\tlist users 'sambaadmin'
\toption status '1'
"""

SAMBASHARE_TEXT_DEVICE = """config sambashare 'u3943892_1'
\toption name 'u3943892_1'
\toption path '/home/u3943892/pool0/data/我的照片'
\tlist users 'sambafw867'
\toption user_force 'u3943892'
"""

SAMBASHARE_TEXT_SCREEN = """config sambashare 'u3943892_1'
\toption name '照片-3943892'
\toption path '/home/u3943892/pool0/data/我的照片'
\tlist users 'sambafw867'
"""

SAMBASHARE_TEXT_SCREEN_MATCH = """config sambashare 'u3943892_nb_9'
\toption name 'u3943892_1'
\toption path '/home/u3943892/pool0/data/我的照片'
\tlist users 'sambafw867'
"""

SAMBAUSER_TEXT = """config sambauser 'u3943892_1'
\tlist dirs '/home/u3943892/pool0/data/下载'
\toption id '1'
\toption name 'fw867'
\toption user 'u3943892'

config sambauser 'u1000001_1'
\tlist dirs 'public'
\toption id '2'
\toption name 'admin'
\toption user 'u1000001'
"""

SAMBA_TEXT = """config samba 'global'
\toption name 'SmartStorage'
\toption workgroup 'WORKGROUP'
"""

USERS_MAP_TEXT = """sambafw867 = fw867
sambaadmin = admin
"""

# 沙箱里的「受管 POSIX 路径」根：引擎里的白名单与共享路径全是 POSIX 语义，
# 测试在 Windows 上必须把这三条映射到临时目录，再用可注入钩子回答文件系统问题。
NAS_DATA_ROOT = '/home/u3943892/pool0/data'
NAS_HOME_ROOT = '/home'
NAS_POOL_ROOT = '/nas'
NAS_MNT_ROOT = '/nas/mnt'
PREFIX_TABLE = (
    (NAS_DATA_ROOT, 'home/u3943892/pool0/data'),
    (NAS_HOME_ROOT, 'home'),
    (NAS_POOL_ROOT, 'nas'),
    # 厂商自己的挂载点在 /mnt/usb-<哈希>（EXTRA_ROOTS 支持 /mnt/usb-* 这种单层通配）
    ('/mnt', 'mnt'),
)
# 前缀必须**从长到短**匹配：/home 比 /home/u3943892/pool0/data 短，
# 先匹配它会把相对部分退化成 Windows 风格路径（`u3943892\pool0\data`）。
PREFIXES_BY_LENGTH = tuple(sorted(PREFIX_TABLE, key=lambda pair: len(pair[0]), reverse=True))


def virtual_of(real) -> str:
    """真实路径 → 沙箱里的受管 POSIX 路径（在沙箱树内时）。"""
    text = str(real).replace('\\', '/')
    for prefix, relative in PREFIX_TABLE:
        if text.endswith('/' + relative):
            return '%s/%s' % (prefix.rstrip('/'), relative)
        head = '%s/' % relative.rstrip('/')
        if head in text:
            return prefix.rstrip('/') + '/' + text.split(head, 1)[1]
    return text


class FakeRunner:
    """假的命令执行器：记录 argv，按预设回答。单测绝不真的执行任何东西。"""

    def __init__(self, answers=None, default=None, handler=None):
        self.calls: list = []
        self.timeouts: list = []
        self.answers = answers or {}
        self.default = default or Result(0, '', '')
        self.handler = handler

    def __call__(self, command, timeout=60):
        key = [str(item) for item in command]
        self.calls.append(key)
        self.timeouts.append(timeout)
        if self.handler is not None:
            answer = self.handler(self, key, timeout)
            if answer is not None:
                return answer
        for pattern, result in self.answers.items():
            if isinstance(pattern, str):
                if str(pattern) in key:
                    return self._materialize(result)
                continue
            if list(pattern) == key:
                return self._materialize(result)
        return self._materialize(self.default)

    @staticmethod
    def _materialize(result):
        if isinstance(result, Result):
            return result
        return Result(*result)

    def argv_for(self, needle, index=0):
        """第 index 条含 needle 的 argv（list）；没有则 None。"""
        found = [list(call) for call in self.calls if needle in call]
        return found[index] if len(found) > index else None

    def count(self, needle):
        return sum(1 for call in self.calls if needle in call)


class FakeResponder:
    """假的 WSD 回应器：不绑端口、不加组播组。"""

    instances: list = []

    def __init__(self, hostname='SmartStorage', workgroup='WORKGROUP', address='',
                 port=5357, state_file='', hello_interval=900, on_log=None, **kwargs):
        self.hostname = hostname
        self.workgroup = workgroup
        self.port = port
        self.state_file = state_file
        self.hello_interval = hello_interval
        self.on_log = on_log or (lambda message: None)
        self.identity = '11111111-2222-3333-4444-555555555555'
        self.address = '192.168.1.8'
        self.started = False
        self.stopped = False
        self.hellos: list = []
        self.fail_start = False
        FakeResponder.instances.append(self)

    @property
    def xaddrs(self):
        return 'http://%s:%d/%s' % (self.address, self.port, self.identity)

    def start(self):
        if self.fail_start:
            raise RuntimeError('端口被占用')
        self.started = True
        return self

    def stop(self):
        self.stopped = True

    def announce_hello(self, times=2, gap=0.3):
        self.hellos.append(times)
        return True


class Sandbox:
    """临时沙箱：把 engine 的路径常量、smb_mgr 与 systemctl 全部换成假的。"""

    def __init__(self, tmp, runner=None, **kwargs):
        self.root = Path(tmp)
        self.data = self.root / 'data'
        self.data.mkdir(parents=True, exist_ok=True)
        (self.root / 'etc' / 'config').mkdir(parents=True, exist_ok=True)
        (self.root / 'etc' / 'samba').mkdir(parents=True, exist_ok=True)
        (self.root / 'var' / 'etc').mkdir(parents=True, exist_ok=True)
        for name in ('home', 'nas', 'mnt'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.fixtures = {
            'etc/config/sambashare': SAMBASHARE_TEXT,
            'etc/config/sambauser': SAMBAUSER_TEXT,
            'etc/config/samba': SAMBA_TEXT,
            'etc/samba/users.map': USERS_MAP_TEXT,
            'var/etc/smb.conf': '[global]\n',
        }
        for name, text in self.fixtures.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        # 假的 3702 占用者：兜底 kill 之后才算释放
        self.wsd_processes = ['1234']
        # 身份自动获取的两个来源：测试给稳定值（改了属性立刻生效），
        # 不依赖跑测试这台机器真的叫什么名字、IP 是多少
        self.system_hostname = 'SmartStorage'
        self.lan_ip = '192.168.1.8'
        self.patches = [
            patch.object(engine, 'APP_ROOT', self.root / 'etc'),
            patch.object(engine, 'VAR_ETC', self.root / 'var' / 'etc'),
            patch.object(engine, 'DATA_DIR', self.data),
            # 路径存在性/真实路径全部走可注入钩子：受管路径是 POSIX 语义，
            # 在 Windows 上只能靠这三条钩子映射到沙箱里的真实目录。
            patch.object(engine, 'path_exists', self.path_exists),
            patch.object(engine, 'path_isdir', self.path_isdir),
            patch.object(engine, 'path_realpath', self.path_realpath),
            patch.object(engine, 'path_listdir', self.path_listdir),
            patch.object(engine, 'path_islink', self.path_islink),
            patch.object(engine, 'path_gethostname', lambda: self.system_hostname),
            patch.object(engine, 'probe_lan_address', lambda: self.lan_ip),
            patch.object(engine, 'SAMBASHARE_CONFIG', self.root / 'etc' / 'config' / 'sambashare'),
            patch.object(engine, 'SAMBAUSER_CONFIG', self.root / 'etc' / 'config' / 'sambauser'),
            patch.object(engine, 'SAMBA_CONFIG', self.root / 'etc' / 'config' / 'samba'),
            patch.object(engine, 'SMB_USER_MAP', self.root / 'etc' / 'samba' / 'users.map'),
            patch.object(engine, 'SMB_CONF', self.root / 'var' / 'etc' / 'smb.conf'),
            patch.object(engine, 'METADATA_PORT', 15357),
            patch.object(engine, 'WSDD_DROPIN_DIR',
                         self.root / 'etc' / 'systemd' / 'system' / 'wsdd.service.d'),
            patch.object(engine, 'WSDD_DROPIN', self.dropin),
            patch.object(engine, 'WSD_PORT_WAIT', 0.0),
            # 回应器启动重试：真机是 5 次 / 1 秒（等官方 wsdd 退出），单测必须打桩成小值
            patch.object(engine, 'RESPONDER_START_RETRIES', 2),
            patch.object(engine, 'RESPONDER_START_GAP', 0.0),
            # 兜底清理命令：真机只有 killall（没有 pkill），显式打桩避免依赖宿主机
            patch.object(engine, 'KILL_CANDIDATES', ()),
            patch.object(engine, 'KILL', '/usr/bin/killall'),
        ]
        for item in self.patches:
            item.start()
        self.runner = runner if runner is not None else FakeRunner()
        self.smb_mgr = str(self.root / 'etc' / 'smb_mgr.sh')
        self.engine = Engine(
            data_dir=self.data,
            runner=(lambda: self.runner),
            responder_factory=FakeResponder,
            samba_mgr=self.smb_mgr,
            systemctl='/bin/systemctl',
            start_responder=False,
            **kwargs)

    def close(self):
        for item in self.patches:
            item.stop()

    # ---- 受管 POSIX 路径 <-> 沙箱真实路径 ---------------------------------
    def real(self, path) -> Path:
        """受管 POSIX 路径 → 沙箱里的真实路径。"""
        text = str(path)
        if text.startswith('/'):
            for prefix, relative in PREFIXES_BY_LENGTH:
                if text == prefix:
                    return self.root / relative
                if text.startswith(prefix + '/'):
                    return self.root / relative / text[len(prefix) + 1:]
            return self.root / 'posix' / text.lstrip('/')
        return Path(text)

    def virtual(self, path) -> str:
        """沙箱真实路径 → 受管 POSIX 路径。"""
        text = str(path).replace('\\', '/')
        root = str(self.root).replace('\\', '/')
        if text == root or text.startswith(root + '/'):
            relative = text[len(root):].lstrip('/')
            for prefix, mapped in PREFIXES_BY_LENGTH:
                if relative == mapped:
                    return prefix
                if relative.startswith(mapped + '/'):
                    return prefix + '/' + relative[len(mapped) + 1:]
            return '/' + relative
        return str(path)

    def makedirs(self, path) -> str:
        """在沙箱里建目录，返回它的受管 POSIX 路径。"""
        target = self.real(path)
        target.mkdir(parents=True, exist_ok=True)
        return self.virtual(target)

    def path_exists(self, path) -> bool:
        return self.real(path).exists()

    def path_isdir(self, path) -> bool:
        return self.real(path).is_dir()

    def path_realpath(self, path) -> str:
        # 先让操作系统解析（真实目录是沙箱里的那个），再映射回受管 POSIX 路径
        return self.virtual(os.path.realpath(str(self.real(path))))

    def path_listdir(self, path) -> list:
        """列目录：只读沙箱里的真实目录，绝不碰宿主机的 /home。"""
        return os.listdir(str(self.real(path)))

    def path_islink(self, path) -> bool:
        return os.path.islink(str(self.real(path)))

    def symlink(self, target, link) -> bool:
        """在沙箱里造符号链接；平台不支持时返回 False（调用方据此 skip）。"""
        link_path = self.real(link)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(self.real(target)), str(link_path))
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True

    # ---- 沙箱内的文件读写 -------------------------------------------------
    @property
    def sambashare(self) -> Path:
        return self.root / 'etc' / 'config' / 'sambashare'

    @property
    def samba_user(self) -> Path:
        return self.root / 'etc' / 'config' / 'sambauser'

    @property
    def smb_conf(self) -> Path:
        return self.root / 'var' / 'etc' / 'smb.conf'

    @property
    def dropin(self) -> Path:
        return self.root / 'etc' / 'systemd' / 'system' / 'wsdd.service.d' / 'netneighbor.conf'


def successful_manager(sandbox, wsd_running=True):
    """让 smb_mgr.sh / systemctl / killall 的调用都成功，并像真脚本那样改动配置文件。

    - `add_dir` 往 sambashare 追加一段（段 id = argv 里的 name、display = share point）、
      往 smb.conf 追加一个以**显示名**为名的 section；
    - `del_dir` 反向删除；
    - `init_config` 按当前 sambashare 重新生成 smb.conf 的 section 列表，
      `netbios name` 也照真脚本那样从 `/etc/config/samba` 的 `option name` 抄过来
      （「恢复为系统主机名」要回读校验它）；
    - `systemctl show -p UnitFileState` 回答单元状态；
    - 3702 的占用状态：兜底 kill 生效或 drop-in 写好之后才算释放。
    """

    def netbios_line():
        """真 smb_mgr.sh 写进 smb.conf 的 `netbios name`（来自 samba 配置的 option name）。"""
        name = engine.config_option(sandbox.root / 'etc' / 'config' / 'samba', 'samba', 'name', '')
        return '\tnetbios name = %s\n' % name if name else ''

    def handler(runner, key, _timeout):
        if 'add_dir' in key:
            at = key.index('add_dir')
            name, path, users = key[at + 1], key[at + 2], key[at + 3]
            display = key[at + 4] if len(key) > at + 4 else name
            with sandbox.sambashare.open('a', encoding='utf-8') as handle:
                handle.write("\nconfig sambashare '%s'\n\toption name '%s'\n"
                             "\toption path '%s'\n\tlist users '%s'\n"
                             % (name, display, path, users))
            with sandbox.smb_conf.open('a', encoding='utf-8') as handle:
                handle.write('[%s]\n\tpath = %s\n' % (display, path))
            return Result(0, 'ok\n', '')
        if 'del_dir' in key:
            name = key[key.index('del_dir') + 1]
            blocks = sandbox.sambashare.read_text(encoding='utf-8').split('config sambashare ')
            kept = [blocks[0]]
            displays = []
            for block in blocks[1:]:
                if block.startswith("'%s'" % name):
                    continue
                kept.append(block)
            sandbox.sambashare.write_text('config sambashare '.join(kept), encoding='utf-8')
            # smb.conf 里的段名是显示名，重新按剩余段生成
            for line in sandbox.sambashare.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('option name '):
                    displays.append(line.split("'")[1])
            sandbox.smb_conf.write_text(
                '[global]\n' + netbios_line()
                + ''.join('[%s]\n' % item for item in displays), encoding='utf-8')
            return Result(0, 'ok\n', '')
        if 'init_config' in key:
            names = []
            for line in sandbox.sambashare.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('option name '):
                    names.append(line.split("'")[1])
            sandbox.smb_conf.write_text(
                '[global]\n' + netbios_line()
                + ''.join('[%s]\n' % name for name in names), encoding='utf-8')
            return Result(0, 'init_config ok\n', '')
        if 'killall' in key or 'pkill' in key:
            sandbox.wsd_processes.clear()
            return Result(0 if wsd_running else 1, '', '')
        if 'reload' in key or 'daemon-reload' in key:
            return Result(0, '', '')
        if 'show' in key:
            return Result(0, ('enabled\n' if sandbox.wsdd_unit_enabled else 'disabled\n'), '')
        if key[:1] == ['/bin/systemctl'] or 'systemctl' in str(key[0]):
            if 'stop' in key:
                return Result(0, '', '')
            if 'start' in key:
                return Result(0, '', '')
        if 'is-active' in key:
            return Result(0, 'active\n', '')
        return Result(0, '', '')

    sandbox.runner.handler = handler
    return sandbox.runner


class EngineHarness(unittest.TestCase):
    """公共脚手架：把引擎放到临时沙箱里，退出时恢复被 patch 的模块常量。"""

    def setUp(self):
        FakeResponder.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sandbox = None

    def build(self, runner=None, **kwargs):
        self.sandbox = Sandbox(self.tmp.name, runner=runner, **kwargs)
        self.addCleanup(self.sandbox.close)
        self.root = self.sandbox.root
        self.runner = self.sandbox.runner
        self.engine = self.sandbox.engine
        # 单测不去真的绑 3702：默认「端口可用」，专门的自检用例再覆盖
        self.addCleanup(patch.object(self.engine, 'wsd_port_free',
                                     return_value=True).stop)
        return self.engine

    def manager(self, wsd_running=True):
        return successful_manager(self.sandbox, wsd_running)

    def data_dir(self):
        """沙箱里 `/home/u3943892/pool0/data` 对应的真实目录。"""
        target = self.root / 'home' / 'u3943892' / 'pool0' / 'data'
        target.mkdir(parents=True, exist_ok=True)
        return target

    def data_child(self, name):
        """在数据根目录下建目录（返回受管 POSIX 路径）。"""
        target = self.data_dir() / name
        target.mkdir(parents=True, exist_ok=True)
        return self.sandbox.virtual(target)

    def data_file(self, name, text='x'):
        """在数据根目录下建文件（返回受管 POSIX 路径）。"""
        target = self.data_dir() / name
        target.write_text(text, encoding='utf-8')
        return self.sandbox.virtual(target)

    def drop_list_dirs(self):
        """去掉 sambauser 里的 `list dirs`。

        真机上 `list dirs` 里是有目录但没有对应共享段的路径（如实报告为 missing）；
        `add_share` 的「这个目录已经是共享」检查会把这类 missing 条目也算进去
        （见报告），所以「新增一个目录」的用例要先把它们清掉。
        """
        kept = [line for line in
                self.sandbox.samba_user.read_text(encoding='utf-8').splitlines(True)
                if not line.strip().startswith('list dirs')]
        self.sandbox.samba_user.write_text(''.join(kept), encoding='utf-8')


class UciParsingTests(EngineHarness):
    def test_parses_sections_options_and_lists(self):
        sections = engine.parse_uci(SAMBAUSER_TEXT)
        self.assertEqual([section['name'] for section in sections], ['u3943892_1', 'u1000001_1'])
        self.assertEqual(sections[0]['type'], 'sambauser')
        self.assertEqual(sections[0]['options']['user'], 'u3943892')
        self.assertEqual(sections[0]['options']['name'], 'fw867')
        self.assertEqual(sections[0]['lists']['dirs'], ['/home/u3943892/pool0/data/下载'])
        self.assertEqual(sections[1]['lists']['dirs'], ['public'])

    def test_ignores_comments_and_blank_lines(self):
        text = "# 注释\n\nconfig samba 'global'\n\toption name 'NAS'\n"
        sections = engine.parse_uci(text)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]['options']['name'], 'NAS')

    def test_anonymous_section_gets_stable_name(self):
        sections = engine.parse_uci("config sambashare\n\toption path '/srv/a'\n")
        self.assertEqual(sections[0]['name'], 'cfg0001')

    def test_double_quoted_values(self):
        sections = engine.parse_uci('config samba "global"\n\toption name "My NAS"\n')
        self.assertEqual(sections[0]['name'], 'global')
        self.assertEqual(sections[0]['options']['name'], 'My NAS')

    def test_parse_dirs_handles_option_and_list(self):
        self.assertEqual(engine.parse_dirs(['a', 'b c']), ['a', 'b', 'c'])
        self.assertEqual(engine.parse_dirs('a b'), ['a', 'b'])
        self.assertEqual(engine.parse_dirs(''), [])

    def test_accounts_and_shares_from_config(self):
        self.build()
        accounts = self.engine.accounts()
        self.assertEqual([item['account'] for item in accounts], ['fw867', 'admin'])
        first = accounts[0]
        self.assertEqual(first['user'], 'u3943892')
        self.assertEqual(first['sambaUser'], 'sambafw867')
        # 归属于账号的共享（靠 sambashare 的 list users 'samba<账号>'），
        # 以及 list dirs 里那条没有共享段的目录（如实报告为 missing）
        self.assertEqual([share['name'] for share in first['shares']], ['u3943892_nb_1', ''])
        share = first['shares'][0]
        self.assertEqual(share['display'], '照片-3943892')
        self.assertEqual(share['path'], '/home/u3943892/pool0/data/我的照片')
        self.assertEqual(share['users'], ['sambafw867'])
        self.assertFalse(share['custom'])
        self.assertFalse(share['deletable'])
        missing = first['shares'][1]
        self.assertTrue(missing['missing'])
        self.assertEqual(missing['path'], '/home/u3943892/pool0/data/下载')
        self.assertEqual(missing['display'], '下载')
        self.assertEqual(accounts[1]['shares'][0]['name'], 'public')

    def test_samba_shares_name_is_section_id_and_display_is_option_name(self):
        """共享的身份是 UCI 段 id；`option name` 只是资源管理器里显示的名字。"""
        self.build()
        shares = self.engine.samba_shares()
        self.assertEqual([item['name'] for item in shares], ['u3943892_nb_1', 'public'])
        self.assertEqual(shares[0]['display'], '照片-3943892')
        self.assertEqual(shares[0]['section'], 'u3943892_nb_1')
        self.assertEqual(shares[0]['users'], ['sambafw867'])
        self.assertEqual(shares[1]['display'], 'public')

    def test_share_in_config_matches_section_id_then_display_name(self):
        """核对分两步：先按段 id 找 sambashare 段，再拿它的显示名比 smb.conf 的段名。"""
        self.build()
        self.sandbox.sambashare.write_text(SAMBASHARE_TEXT_DEVICE, encoding='utf-8')
        # 段 id 与显示名一样时命中
        self.sandbox.smb_conf.write_text('[global]\n[u3943892_1]\n', encoding='utf-8')
        self.assertTrue(self.engine.share_in_config('u3943892_1'))
        # 段 id 在 sambashare 里没有：直接 False
        self.assertFalse(self.engine.share_in_config('u3943892_nb_1'))

        self.sandbox.sambashare.write_text(SAMBASHARE_TEXT_SCREEN, encoding='utf-8')
        # 真机形态：段 id u3943892_1 → 显示名 照片-3943892 → smb.conf 段名也是它
        self.sandbox.smb_conf.write_text('[global]\n[照片-3943892]\n', encoding='utf-8')
        self.assertTrue(self.engine.share_in_config('u3943892_1'))
        # 拿段 id 去比 smb.conf 会永远为 False（真机踩过的坑）
        self.assertFalse(self.engine.share_in_config('照片-3943892'))

        # 段 id 与显示名互换也要能区分
        self.sandbox.sambashare.write_text(SAMBASHARE_TEXT_SCREEN_MATCH, encoding='utf-8')
        self.sandbox.smb_conf.write_text('[global]\n[u3943892_1]\n', encoding='utf-8')
        self.assertTrue(self.engine.share_in_config('u3943892_nb_9'))
        self.assertFalse(self.engine.share_in_config('u3943892_1'))

    def test_smb_user_mapping(self):
        self.build()
        mapping = self.engine.user_map()
        self.assertEqual(mapping['sambafw867'], 'fw867')
        self.assertEqual(engine.account_to_smb_user('fw867', mapping), 'sambafw867')
        self.assertEqual(engine.account_to_smb_user('admin', mapping), 'sambaadmin')
        self.assertEqual(engine.account_to_smb_user('newbie', mapping), 'sambanewbie')

    def test_smb_conf_sections(self):
        self.assertEqual(engine.smb_conf_sections('[global]\n[照片-3943892]\n\tpath = /x\n'),
                         ['global', '照片-3943892'])

    def test_config_option_falls_back_to_file(self):
        self.build()
        # 第二个参数是 **uci section 类型**（`config samba 'global'` 里类型是 samba、
        # 段名是 global），不是段名。
        self.assertEqual(engine.config_option(engine.SAMBA_CONFIG, 'samba', 'name'), 'SmartStorage')
        self.assertEqual(
            engine.config_option(engine.SAMBA_CONFIG, 'samba', 'missing', 'fallback'), 'fallback')

    def test_missing_config_is_reported_not_crashed(self):
        self.build()
        self.sandbox.samba_user.unlink()
        with self.assertRaises(Error) as caught:
            self.engine.accounts()
        self.assertIn('读取配置失败', str(caught.exception))


class ShareNameTests(unittest.TestCase):
    def test_plugin_share_name_starts_at_one(self):
        self.assertEqual(engine.plugin_share_name('fw867', []), 'fw867_nb_1')

    def test_plugin_share_name_skips_taken(self):
        taken = ['u3943892_1', 'fw867_nb_1', 'fw867_nb_2', 'public']
        self.assertEqual(engine.plugin_share_name('fw867', taken), 'fw867_nb_3')

    def test_plugin_share_name_never_collides_with_app_namespace(self):
        taken = ['fw867_1', 'fw867_2', 'fw867_10']
        self.assertEqual(engine.plugin_share_name('fw867', taken), 'fw867_nb_1')

    def test_share_name_part_sanitizes(self):
        self.assertEqual(engine.share_name_part('fw 867/x'), 'fw_867_x')
        self.assertEqual(engine.share_name_part(''), 'user')

    def test_is_plugin_share(self):
        self.assertTrue(engine.is_plugin_share('fw867_nb_1', 'fw867'))
        self.assertTrue(engine.is_plugin_share('fw867_nb_12', 'fw867'))
        self.assertFalse(engine.is_plugin_share('fw867_1', 'fw867'))
        self.assertFalse(engine.is_plugin_share('admin_nb_1', 'fw867'))
        self.assertFalse(engine.is_plugin_share('public', 'admin'))
        self.assertTrue(engine.is_plugin_share('fw867_nb_1'))

    def test_validate_share_name(self):
        # 真机的显示名可以是中文（照片-3943892），只有空白/分隔符/方括号之类才非法。
        # 段 id 一律是 ASCII（engine.plugin_share_name 生成），所以这里不涉及中文段 id。
        self.assertEqual(engine.validate_share_name('照片-3943892'), '照片-3943892')
        self.assertEqual(engine.validate_share_name('fw867_nb_1'), 'fw867_nb_1')
        for bad in ['', '  ', 'a b', 'x' * 65, 'a/b', 'a[b]']:
            with self.subTest(bad=bad), self.assertRaises(Error):
                engine.validate_share_name(bad)


class AllowedRootTests(unittest.TestCase):
    def test_derives_roots_from_existing_config(self):
        sections = {
            'sambauser': engine.parse_uci(SAMBAUSER_TEXT),
            'sambashare': engine.parse_uci(SAMBASHARE_TEXT),
        }
        # `data_root_for` 用 pathlib 拼「挂载点 + /home/<uXXXX>/pool0/data」，
        # 在 Windows 上会拼出反斜杠路径，无法验证 POSIX 白名单语义，
        # 所以这里把这两个「按平台拼路径」的小工具打桩成 POSIX 形态
        # （Linux 上它们本来就返回这些值）。其余推导逻辑都跑真实实现。
        def posix_data_root(_root, user_id):
            return '/home/%s/pool0/data' % user_id

        def posix_paths_root(_root):
            return '/nas/pool0'

        with patch.object(engine, 'data_root_for', posix_data_root), \
                patch.object(engine, 'paths_root_for', posix_paths_root):
            roots = engine.derive_allowed_roots(sections)
        self.assertIn('/home/u3943892/pool0/data', roots)      # 来自 sambauser 的 option user
        self.assertIn('/home/u1000001/pool0/data', roots)
        # 已有共享的 path 取**父目录**（`/nas/pool0/public` → `/nas/pool0`）
        self.assertIn('/nas/pool0', roots)
        self.assertNotIn('/nas/pool0/public', roots)
        self.assertTrue(all(root.startswith('/') for root in roots))

    def test_data_root_for_uses_the_nas_mount_layout(self):
        """真机布局：<挂载点>/home/<NAS用户>/pool0/data。"""
        self.assertEqual(engine.data_root_for(Path('/'), 'u3943892'),
                         str(Path('/') / 'home' / 'u3943892' / 'pool0' / 'data'))
        self.assertEqual(engine.data_root_for(Path('/'), ''), '')

    def test_env_override_wins(self):
        # 额外根在这里显式隔离（EXTRA_ROOTS=''）：这条用例只管 ALLOWED_ROOTS 的覆盖语义，
        # 断言必须与环境无关——真机 NAS 上 `/nas/mnt` 是存在的，不隔离就会多出一项
        with patch.dict(os.environ, {'ALLOWED_ROOTS': '/data/a:/data/b'}), \
                patch.object(engine, 'EXTRA_ROOTS', engine.parse_extra_roots('')):
            self.assertEqual(engine.resolve_allowed_roots(None), ['/data/a', '/data/b'])

    def test_fallback_when_config_is_empty(self):
        # 同上：隔离额外根，避免「跑测试这台机器上有没有 /nas/mnt」影响结果
        with patch.dict(os.environ, {'ALLOWED_ROOTS': ''}), \
                patch.object(engine, 'EXTRA_ROOTS', engine.parse_extra_roots('')):
            roots = engine.resolve_allowed_roots({'sambauser': [], 'sambashare': []})
        self.assertIn('/nas/pool0', roots)
        self.assertIn('/home/*/pool0/data', roots)

    def test_root_match(self):
        self.assertTrue(engine.root_match('/home/u3943892/pool0/data', '/home/*/pool0/data'))
        self.assertFalse(engine.root_match('/home/u3943892/pool0/other', '/home/*/pool0/data'))
        self.assertTrue(engine.root_match('/nas/pool0', '/nas/pool0'))
        self.assertTrue(engine.root_match('/nas/pool0/a/b', '/nas/pool0'))
        self.assertFalse(engine.root_match('/nas/pool1/a', '/nas/pool0'))

    def test_engine_uses_config_derived_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            roots = sandbox.engine.allowed_roots()
        self.assertIn('/home/u3943892/pool0/data', roots)
        self.assertIn('/nas/pool0', roots)


class ExtraRootTests(unittest.TestCase):
    """外接设备（`EXTRA_ROOTS`，默认 `/nas/mnt`）：**追加**来源，且只列当前存在的根。"""

    @staticmethod
    def extra_roots_from_env(value=None, unset=False):
        """在**干净环境**的子进程里导入 engine，返回它解析出来的 `EXTRA_ROOTS`。

        这样验的是「环境变量 → 常量」这一步（模块导入时解析），而不是当前进程里
        被测试改过的状态；`EXTRA_ROOTS` 的当前进程值不受影响。
        """
        project = Path(__file__).resolve().parent.parent
        environment = {key: item for key, item in os.environ.items()
                       if key != 'EXTRA_ROOTS'}
        if not unset:
            environment['EXTRA_ROOTS'] = '' if value is None else value
        completed = subprocess.run(
            [sys.executable, '-c', 'import engine; print(":".join(engine.EXTRA_ROOTS))'],
            cwd=str(project), capture_output=True, text=True, env=environment, timeout=120)
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def test_default_extra_roots_is_nas_mnt(self):
        """没设 EXTRA_ROOTS 时默认追加 `/nas/mnt`（真机 usb/pa0/pa1 都在它下面）。"""
        self.assertEqual(self.extra_roots_from_env(unset=True), '/nas/mnt')

    def test_empty_extra_roots_env_disables_all_extras(self):
        """显式空串 → 不追加任何额外根（`ALLOWED_ROOTS=` 时它就是严格的最终白名单）。"""
        self.assertEqual(self.extra_roots_from_env(''), '')

    def test_custom_extra_roots_env_is_split_on_colons(self):
        self.assertEqual(self.extra_roots_from_env('/nas/mnt:/mnt/usb-*'),
                         '/nas/mnt:/mnt/usb-*')

    def test_parse_extra_roots_handles_empty_and_blank_items(self):
        self.assertEqual(engine.parse_extra_roots(''), ())
        self.assertEqual(engine.parse_extra_roots(None), ())
        self.assertEqual(engine.parse_extra_roots('  '), ())
        self.assertEqual(engine.parse_extra_roots('/a::/b:'), ('/a', '/b'))
        self.assertEqual(engine.parse_extra_roots('/nas/mnt'), ('/nas/mnt',))

    def test_empty_extra_roots_keeps_allowed_roots_strict(self):
        """`EXTRA_ROOTS=` 时最终白名单**不含** `/nas/mnt`，即使它真的存在。"""
        with patch.dict(os.environ, {'ALLOWED_ROOTS': '/data/a:/data/b'}), \
                patch.object(engine, 'EXTRA_ROOTS', engine.parse_extra_roots('')), \
                patch.object(engine, 'path_isdir', lambda path: True):
            self.assertEqual(engine.extra_roots(), [])
            self.assertEqual(engine.resolve_allowed_roots(None), ['/data/a', '/data/b'])

    def test_nas_mnt_is_appended_when_it_exists(self):
        """`EXTRA_ROOTS` 未设置（默认 `/nas/mnt`）且它存在时，白名单与状态里都含它。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            with patch.object(engine, 'EXTRA_ROOTS',
                              engine.parse_extra_roots('/nas/mnt')):
                sandbox.makedirs('/nas/mnt/usb')
                self.assertIn('/nas/mnt', sandbox.engine.allowed_roots())
                self.assertIn('/nas/mnt', sandbox.engine.status()['allowedRoots'])

    def test_resolve_appends_extra_roots_to_the_derived_list(self):
        with patch.dict(os.environ, {'ALLOWED_ROOTS': ''}), \
                patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)), \
                patch.object(engine, 'path_isdir', lambda path: str(path) == '/nas/mnt'):
            roots = engine.resolve_allowed_roots({'sambauser': [], 'sambashare': []})
        self.assertIn('/home/*/pool0/data', roots)          # 兜底 + 按配置派生那份没变
        self.assertIn('/nas/pool0', roots)
        self.assertEqual(roots[-1], '/nas/mnt')             # 追加在最后
        self.assertEqual(roots.count('/nas/mnt'), 1)        # 去重

    def test_allowed_roots_override_still_appends_extra_roots(self):
        """`ALLOWED_ROOTS` 仍然是显式覆盖（给了就只用它），但 EXTRA_ROOTS 照旧追加。"""
        with patch.dict(os.environ, {'ALLOWED_ROOTS': '/data/a:/data/b'}), \
                patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)), \
                patch.object(engine, 'path_isdir', lambda path: str(path) == '/nas/mnt'):
            roots = engine.resolve_allowed_roots(None)
        self.assertEqual(roots, ['/data/a', '/data/b', '/nas/mnt'])

    def test_missing_extra_root_is_not_listed(self):
        """根不存在（拔盘）时不纳入：不给出一个指向空路径的白名单根。"""
        with patch.dict(os.environ, {'ALLOWED_ROOTS': ''}), \
                patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt', '/nas/mnt/usb')), \
                patch.object(engine, 'path_isdir', lambda path: False):
            self.assertEqual(engine.extra_roots(), [])
            roots = engine.resolve_allowed_roots(None)
        self.assertNotIn('/nas/mnt', roots)

    def test_engine_cache_follows_plug_and_unplug(self):
        """允许根缓存要能反映「外接设备在不在」：不 force 也能看出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
                self.assertNotIn('/nas/mnt', sandbox.engine.allowed_roots())
                sandbox.makedirs('/nas/mnt/usb')                    # 插上 U 盘
                self.assertIn('/nas/mnt', sandbox.engine.allowed_roots())
                shutil.rmtree(str(sandbox.real('/nas/mnt')))        # 拔掉
                self.assertNotIn('/nas/mnt', sandbox.engine.allowed_roots())

    def test_custom_extra_roots_support_vendor_wildcards(self):
        """EXTRA_ROOTS 可自定义，`/mnt/usb-*` 这种厂商挂载点用单层通配收进来。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt', '/mnt/usb-*')):
                self.assertEqual(engine.extra_roots(), [])          # 一个都不在
                sandbox.makedirs('/mnt/usb-1a2b3c')
                (sandbox.real('/mnt/usb-1a2b3c') / '照片').mkdir()
                roots = sandbox.engine.allowed_roots()
                self.assertIn('/mnt/usb-1a2b3c', roots)
                self.assertNotIn('/nas/mnt', roots)                 # 没插上的不列
                # 厂商挂载点下的目录同样能通过校验
                self.assertEqual(
                    engine.validate_share_path('/mnt/usb-1a2b3c/照片', roots),
                    '/mnt/usb-1a2b3c/照片')
                self.assertTrue(engine.within_roots('/mnt/usb-1a2b3c/照片', roots))

    def test_status_allowed_roots_include_the_extra_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
                sandbox.makedirs('/nas/mnt')
                self.assertIn('/nas/mnt', sandbox.engine.status()['allowedRoots'])

    def test_existing_share_survives_unplugging_the_device(self):
        """拔盘后共享仍然保留（不报错），弹窗把该位置标成「未接入」。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            successful_manager(sandbox)
            sandbox.makedirs(NAS_DATA_ROOT)                          # 账号数据根
            with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
                target = sandbox.makedirs('/nas/mnt/usb/下载')
                sandbox.engine.add_share('fw867', target)
                shutil.rmtree(str(sandbox.real('/nas/mnt')))         # 拔盘：路径整个没了

                snapshot = sandbox.engine.status()                   # 不能报错
                data = sandbox.engine.browse_account_dirs('fw867')

        self.assertNotIn('/nas/mnt', snapshot['allowedRoots'])
        paths = [share['path'] for account in snapshot['accounts']
                 for share in account['shares']]
        self.assertIn(target, paths)                                 # 共享还在
        extra = next(item for item in data['locations'] if item['root'] == '/nas/mnt')
        self.assertFalse(extra['available'])                         # 弹窗显示「未接入」
        self.assertEqual(extra['dirs'], [])


class LocationLabelTests(unittest.TestCase):
    """弹窗「位置」分组的标签规则（纯函数，见 `engine.location_label`）。"""

    def test_external_devices_are_labelled(self):
        for path in ('/nas/mnt/usb', '/nas/mnt/usb/下载', '/nas/mnt/usb-1a2b',
                     '/mnt/usb-1a2b3c', '/mnt/usb-1a2b3c/照片'):
            with self.subTest(path=path):
                self.assertEqual(engine.location_label(path), '外接设备')

    def test_other_nas_mnt_paths_use_the_last_segment(self):
        self.assertEqual(engine.location_label('/nas/mnt'), 'mnt')
        self.assertEqual(engine.location_label('/nas/mnt/pa0'), 'pa0')
        self.assertEqual(engine.location_label('/nas/mnt/pa1'), 'pa1')

    def test_pool_paths_are_labelled(self):
        for path in ('/nas/pool0', '/nas/pool0/公开', NAS_DATA_ROOT,
                     NAS_DATA_ROOT + '/照片'):
            with self.subTest(path=path):
                self.assertEqual(engine.location_label(path), '存储池')

    def test_unknown_root_uses_the_last_segment(self):
        self.assertEqual(engine.location_label('/data/media'), 'media')
        self.assertEqual(engine.location_label('/nas/mnt-pa'), 'mnt-pa')
        self.assertEqual(engine.location_label('/nas/mnt/'), 'mnt')


class PathValidationTests(EngineHarness):
    def test_accepts_directory_inside_allowed_root(self):
        self.build()
        target = self.data_dir() / '照片'
        target.mkdir()
        # `/home/*/pool0/data` 是「恰好等于该根」的匹配（root_match 的 * 只吃一层），
        # 所以子目录要落在具体用户根 `/home/u3943892/pool0/data` 里才算数。
        value = engine.validate_share_path(
            self.sandbox.virtual(target), ['/home/u3943892/pool0/data'])
        self.assertEqual(value, '/home/u3943892/pool0/data/照片')
        self.assertFalse(engine.within_roots('/home/u3943892/pool0/data/照片',
                                             ['/home/*/pool0/data']))
        self.assertTrue(engine.within_roots('/home/u3943892/pool0/data/照片',
                                            ['/home/u3943892/pool0/data']))
        # 引擎从配置推导出来的白名单也要认这个子目录
        self.assertTrue(engine.within_roots('/home/u3943892/pool0/data/照片',
                                            self.engine.allowed_roots()))

    def test_accepts_directories_under_nas_mnt(self):
        """外接设备在 `/nas/mnt` 下：EXTRA_ROOTS 生效时它和它的子目录都能共享。"""
        self.build()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            target = self.sandbox.makedirs('/nas/mnt/usb/下载')
            roots = self.engine.allowed_roots()
            self.assertIn('/nas/mnt', roots)
            self.assertEqual(engine.validate_share_path(target, roots), '/nas/mnt/usb/下载')
            self.assertEqual(engine.validate_share_path('/nas/mnt', roots), '/nas/mnt')
            # 没有这个追加根时同一条路径必须被挡住（白名单真的在起作用）
            with patch.object(engine, 'EXTRA_ROOTS', ()):
                strict = self.engine.allowed_roots(force=True)
        self.assertNotIn('/nas/mnt', strict)
        with self.assertRaises(Error) as caught:
            engine.validate_share_path(target, strict)
        self.assertIn('不在允许的根目录内', str(caught.exception))

    def test_rejects_relative_path(self):
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('home/data', ['/home/*/pool0/data'])
        self.assertIn('绝对路径', str(caught.exception))
    def test_rejects_dotdot(self):
        """带 `..` 的越界路径必须被挡住。

        实测 `posixpath.normpath` 会先把绝对路径里的 `..` 折叠掉
        （`/nas/pool0/../etc` → `/nas/etc`），所以「不允许出现 ..」那一关对绝对路径
        其实不可达，真正挡住越界的是**规范化之后的白名单比对**。
        """
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('/nas/pool0/../etc', ['/nas/pool0'])
        self.assertIn('不在允许的根目录内', str(caught.exception))
        self.assertEqual(posixpath.normpath('/nas/pool0/../etc'), '/nas/etc')
        # 折叠后仍落在白名单内时，只能靠文件系统探测兜底（真实目录 /nas/pool0/other 不存在）
        self.build()
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('/nas/pool0/sub/../other', [NAS_POOL_ROOT])
        self.assertIn('不存在', str(caught.exception))
        # 规范化会把尾巴上的 `..` 整段吃掉（不再是越界路径）
        self.assertEqual(posixpath.normpath('/nas/pool0/../..'), '/')

    def test_rejects_outside_allowlist(self):
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('/etc', ['/nas/pool0'])
        self.assertIn('不在允许的根目录内', str(caught.exception))

    def test_rejects_empty(self):
        with self.assertRaises(Error):
            engine.validate_share_path('', ['/nas/pool0'])

    def test_rejects_missing_and_file(self):
        self.build()
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('/nas/pool0/nope', [NAS_POOL_ROOT])
        self.assertIn('不存在', str(caught.exception))
        pool = self.sandbox.makedirs('/nas/pool0')          # 建出白名单里的根，再放一个文件
        target = self.root / 'nas' / 'pool0' / 'afile'
        target.write_text('x', encoding='utf-8')
        self.assertEqual(pool, '/nas/pool0')
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('/nas/pool0/afile', [NAS_POOL_ROOT])
        self.assertIn('不是目录', str(caught.exception))

    def test_rejects_symlink_escaping_the_root(self):
        self.build()
        inside = self.root / 'pool0'
        inside.mkdir(exist_ok=True)
        outside = self.root / 'secret'
        outside.mkdir(exist_ok=True)
        link = inside / 'link'
        try:
            os.symlink(str(outside), str(link))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('当前平台不支持创建符号链接')
        with self.assertRaises(Error) as caught:
            engine.validate_share_path('/pool0/link', ['/pool0'])
        self.assertIn('真实路径', str(caught.exception))


class UserListTests(unittest.TestCase):
    def test_accepts_account_names(self):
        self.assertEqual(engine.validate_user_list('fw867'), 'fw867')
        self.assertEqual(engine.validate_user_list('fw867 admin'), 'fw867 admin')
        self.assertEqual(engine.validate_user_list(['fw867', 'admin']), 'fw867 admin')
        self.assertEqual(engine.validate_user_list('', default='fw867'), 'fw867')

    def test_rejects_samba_prefixed_names(self):
        """实测：传 sambafw867 会在 pdbedit 那步失败，所以必须挡住。"""
        with self.assertRaises(Error) as caught:
            engine.validate_user_list('sambafw867')
        self.assertIn('不要填 SMB 账号', str(caught.exception))

    def test_rejects_junk(self):
        for bad in ['', '   ', 'a b/c', 'x' * 40, '中文']:
            with self.subTest(bad=bad), self.assertRaises(Error):
                engine.validate_user_list(bad)

    def test_force_user_accepts_nas_user_and_auto(self):
        self.assertEqual(engine.validate_force_user('u3943892'), 'u3943892')
        self.assertEqual(engine.validate_force_user('auto'), '')
        self.assertEqual(engine.validate_force_user(''), '')
        with self.assertRaises(Error):
            engine.validate_force_user('u 123')


class CommandLineTests(unittest.TestCase):
    def test_add_dir_argv_matches_documented_signature(self):
        command = engine.add_dir_command(
            'u3943892_nb_1', '/home/u3943892/pool0/data/我的照片', 'fw867',
            '照片-3943892', 'u3943892', smb_mgr='/usr/bin/smb_mgr.sh')
        self.assertEqual(command, [
            '/usr/bin/smb_mgr.sh', 'shares', 'add_dir', 'u3943892_nb_1',
            '/home/u3943892/pool0/data/我的照片', 'fw867', '照片-3943892', 'u3943892'])

    def test_add_dir_argv_omits_force_user_when_empty(self):
        command = engine.add_dir_command('fw867_nb_1', '/nas/pool0/a', 'fw867',
                                         smb_mgr='/usr/bin/smb_mgr.sh')
        self.assertEqual(command[-1], 'fw867_nb_1')      # 默认 share_point = share_name
        self.assertEqual(len(command), 7)

    def test_delete_and_reload_argv(self):
        self.assertEqual(engine.del_dir_command('fw867_nb_1', smb_mgr='/usr/bin/smb_mgr.sh'),
                         ['/usr/bin/smb_mgr.sh', 'shares', 'del_dir', 'fw867_nb_1'])
        self.assertEqual(engine.init_config_command(smb_mgr='/usr/bin/smb_mgr.sh'),
                         ['/usr/bin/smb_mgr.sh', 'init_config'])
        self.assertEqual(engine.reload_units_command(), ['systemctl', 'reload', 'smb', 'nmb'])


class WsddLifecycleTests(EngineHarness):
    def test_takeover_command_sequence(self):
        """接管顺序：stop wsdd → 兜底 kill → 写 drop-in → daemon-reload → 起回应器。

        真机（192.168.1.8）没有 pkill，只有 killall，所以兜底用的是 killall；
        drop-in 是直接写文件、不产生命令调用。
        """
        self.build()
        self.manager()
        self.engine.start()
        self.assertEqual(self.runner.calls[0], ['/bin/systemctl', 'stop', 'wsdd'])
        self.assertEqual(self.runner.calls[1], ['/usr/bin/killall', 'wsdd'])
        self.assertEqual(self.runner.calls[2], ['/bin/systemctl', 'daemon-reload'])
        self.assertFalse(any('pkill' in call for call in self.runner.calls))
        self.assertTrue(self.engine.discovery['managed'])
        self.assertTrue(self.engine.discovery['dropin'])

    def test_takeover_writes_the_noop_dropin(self):
        self.build()
        self.manager()
        self.engine.start()
        text = self.sandbox.dropin.read_text(encoding='utf-8')
        self.assertIn('[Service]', text)
        self.assertIn('ExecStart=', text)
        self.assertIn('ExecStart=/bin/true', text)
        self.assertIn('ExecStop=', text)
        self.assertIn('ExecStop=/bin/true', text)
        self.assertIn('ExecReload=/bin/true', text)
        self.assertIn('RemainAfterExit=yes', text)
        # 只动我们自己的那个文件，绝不碰 /usr/lib 下的原 unit
        self.assertEqual(
            [item.name for item in self.sandbox.dropin.parent.iterdir()],
            ['netneighbor.conf'])

    def test_takeover_waits_for_3702_and_starts_responder_after(self):
        seen = []

        def free():
            seen.append(len(self.runner.calls))
            return len(self.runner.calls) >= 3     # 只有 stop/kill/drop-in 做完才算释放

        self.build()
        self.manager()
        with patch.object(self.engine, 'wsd_port_free', side_effect=free):
            self.engine.start()
        self.assertTrue(seen)
        self.assertGreaterEqual(seen[0], 3)        # 端口自检发生在接管步骤之后
        self.assertIsNotNone(self.engine.responder)
        self.assertTrue(self.engine.responder.started)

    def test_takeover_does_not_use_mask(self):
        """回归：用 mask 会让官方 smb_mgr.sh 的 restart wsdd 失败，所以不许再用。"""
        self.build()
        self.manager()
        self.engine.start()
        self.assertFalse(any('mask' in call[1] for call in self.runner.calls
                             if call[0] == '/bin/systemctl'))

    def test_takeover_logs_every_step(self):
        self.build()
        self.manager()
        self.engine.start()
        text = '\n'.join(self.engine.recent_log(50))
        for needle in ('停止官方 wsdd成功', '清理残留的 wsdd 进程', 'drop-in', 'daemon-reload'):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_takeover_reports_port_still_busy(self):
        self.build()
        self.manager()
        with patch.object(self.engine, 'wsd_port_free', return_value=False), \
                patch.object(self.engine, 'wsd_port_holder', return_value='UNCONN 0 0 0.0.0.0:3702 users:(("wsdd",pid=1,fd=3))'):
            self.engine.start()
        text = '\n'.join(self.engine.recent_log(50))
        self.assertIn('3702/udp 仍被占用', text)
        self.assertIn('wsdd', text)

    def test_takeover_retries_once_when_port_is_still_busy(self):
        """实测竞态：停服时 ExecStopPost --restore-wsdd 会把官方 wsdd 拉回来，
        所以 3702 还占着时要**整套重试一遍**（stop→kill→drop-in→daemon-reload）。"""
        calls = {'free': 0}

        def free():
            calls['free'] += 1
            return calls['free'] > 1        # 第一轮仍被占用，第二轮才释放

        self.build()
        self.manager()
        with patch.object(self.engine, 'wsd_port_free', side_effect=free):
            self.engine.start()
        # FakeRunner.count 是子串匹配，用完整 argv 精确计数
        self.assertEqual(self.runner.calls.count(['/bin/systemctl', 'stop', 'wsdd']), 2)
        self.assertEqual(self.runner.calls.count(['/usr/bin/killall', 'wsdd']), 2)
        self.assertEqual(self.runner.calls.count(['/bin/systemctl', 'daemon-reload']), 2)
        text = '\n'.join(self.engine.recent_log(50))
        self.assertIn('3702 仍被占用，重试一次停止流程', text)
        self.assertTrue(self.engine.discovery['running'])

    def test_restore_command_sequence(self):
        """还原：删 drop-in → daemon-reload → start wsdd。"""
        self.build()
        self.manager()
        self.engine.start()
        self.runner.calls.clear()
        message = self.engine.wsdd_restore()
        self.assertIn('已删除 drop-in', message)
        self.assertIn('启动官方 wsdd成功', message)
        self.assertEqual(self.runner.calls, [
            ['/bin/systemctl', 'daemon-reload'],
            ['/bin/systemctl', 'start', 'wsdd'],
        ])
        self.assertFalse(self.sandbox.dropin.exists())
        self.assertFalse(self.engine.discovery['managed'])
        self.assertFalse(self.engine.discovery['dropin'])

    def test_restore_is_idempotent(self):
        self.build()
        self.manager()
        self.engine.start()
        self.engine.wsdd_restore()
        self.runner.calls.clear()
        message = self.engine.wsdd_restore()          # 再来一次不能报错
        self.assertIn('没有需要清理的 drop-in', message)
        self.assertEqual(self.runner.calls, [
            ['/bin/systemctl', 'daemon-reload'],
            ['/bin/systemctl', 'start', 'wsdd'],
        ])

    def test_restore_survives_systemctl_errors(self):
        """ExecStopPost 里跑，绝不能因为命令报错而抛异常。"""
        self.build(runner=FakeRunner(default=Result(1, '', 'systemd 不可用')))
        message = self.engine.wsdd_restore()
        self.assertIsInstance(message, str)
        self.assertIn('systemd 不可用', message)

    def test_restore_stops_our_responder_first(self):
        self.build()
        self.manager()
        self.engine.start()
        responder = self.engine.responder
        self.engine.wsdd_restore()
        self.assertTrue(responder.stopped)            # 先发 Bye 再放开 3702
        self.assertIsNone(self.engine.responder)

    def test_start_launches_responder_and_stop_sends_bye(self):
        self.build()
        self.manager()
        self.engine.start()
        self.assertTrue(self.engine.discovery['running'])
        responder = self.engine.responder
        self.assertIsInstance(responder, FakeResponder)
        self.assertTrue(responder.started)
        self.assertEqual(responder.state_file, str(self.engine.identity_file))
        self.assertEqual(responder.port, 15357)
        self.assertEqual(responder.hostname, 'SmartStorage')
        self.engine.stop_responder()
        self.assertTrue(responder.stopped)
        self.assertFalse(self.engine.discovery['running'])

    def test_responder_failure_does_not_break_the_plugin(self):
        class Broken(FakeResponder):
            def start(self):
                raise RuntimeError('3702 被占用')

        self.build()
        self.manager()
        with patch.object(self.engine, 'responder_factory', Broken):
            self.engine.start()
        self.assertFalse(self.engine.discovery['running'])
        self.assertIn('3702 被占用', self.engine.discovery['msg'])
        # 重试次数按打桩的小值（2 次）重试，每次都要新建实例；
        # 失败的实例由 start() 自己 stop() 清理（见 WsdResponderStartFailureTests）
        self.assertEqual(len(FakeResponder.instances), 2)
        self.assertIsNone(self.engine.responder)

    def test_start_retries_responder_then_succeeds(self):
        """真机上「停官方 wsdd」与「自己绑 3702」有竞态：前几次失败要接着重试。"""
        state = {'tries': 0}

        class Flaky(FakeResponder):
            def start(self):
                state['tries'] += 1
                if state['tries'] == 1:
                    raise RuntimeError('Address already in use')
                return FakeResponder.start(self)

        self.build()
        self.manager()
        with patch.object(self.engine, 'responder_factory', Flaky):
            self.engine.start()
        self.assertEqual(state['tries'], 2)
        self.assertTrue(self.engine.discovery['running'])
        self.assertIsNotNone(self.engine.responder)
        text = '\n'.join(self.engine.recent_log(50))
        self.assertIn('WSD 回应器第 1 次启动失败', text)

    def test_metadata_post_counting(self):
        self.build()
        self.manager()
        self.engine.start()
        self.engine.on_responder_log('wsd http: POST /abc from 192.168.1.5 action=Get')
        self.engine.on_responder_log('wsd http: GET /abc from 192.168.1.5')
        self.engine.on_responder_log('wsd http: POST /abc from 192.168.1.5 action=Bye')
        self.assertEqual(self.engine.discovery['metadataPosts'], 1)
        self.assertGreater(self.engine.discovery['lastMetadataAt'], 0)

    def test_restart_responder_announces_hello(self):
        self.build()
        self.manager()
        self.engine.start()
        first = self.engine.responder
        snapshot = self.engine.restart_responder(times=2)
        self.assertTrue(first.stopped)
        self.assertIsNot(self.engine.responder, first)
        self.assertEqual(self.engine.responder.hellos, [2])
        self.assertTrue(snapshot['discovery']['running'])
        self.assertEqual(snapshot['discovery']['message'], '已重新宣告')

    def test_shutdown_stops_responder_and_restores_wsdd(self):
        self.build()
        self.manager()
        self.engine.start()
        responder = self.engine.responder
        self.engine.shutdown()
        self.assertTrue(responder.stopped)
        self.assertEqual(self.runner.argv_for('start'), ['/bin/systemctl', 'start', 'wsdd'])
        self.assertFalse(self.sandbox.dropin.exists())


class AddShareTests(EngineHarness):
    def prepare_target(self, name='新照片'):
        """建好沙箱 + 假 smb_mgr，返回一个还没被共享过的沙箱目录（受管 POSIX 路径）。

        注意别用 `/home/u3943892/pool0/data/我的照片`：默认配置里它已经被段 id
        `u3943892_nb_1` 共享，会被 add_share 的重复检查挡住。
        """
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_dir() / name
        target.mkdir()
        return self.sandbox.virtual(target)

    def test_add_share_builds_expected_argv_and_verifies(self):
        target = self.prepare_target()
        result = self.engine.add_share('fw867', target)
        argv = self.runner.argv_for('add_dir')
        # 段 id 是插件命名空间的 fw867_nb_1；中文只出现在共享显示名（share point）里
        self.assertEqual(argv, [self.sandbox.smb_mgr, 'shares', 'add_dir', 'fw867_nb_1',
                                '/home/u3943892/pool0/data/新照片', 'fw867',
                                '新照片', 'u3943892'])
        self.assertEqual(result['shareName'], 'fw867_nb_1')
        self.assertEqual(result['sharePoint'], '新照片')
        self.assertEqual(result['users'], ['fw867'])
        self.assertEqual(result['forceUser'], 'u3943892')
        self.assertTrue(result['verified'])
        self.assertTrue(result['inConfig'])
        # 段 id 与显示名都能在配置里对上
        share = next(item for item in self.engine.shares() if item['name'] == 'fw867_nb_1')
        self.assertEqual(share['display'], '新照片')
        self.assertTrue(share['custom'])

    def test_add_share_accepts_a_directory_under_nas_mnt(self):
        """外接设备（`/nas/mnt` 下）的目录也能共享：EXTRA_ROOTS 是追加的白名单根。"""
        self.build()
        self.manager()
        self.drop_list_dirs()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            target = self.sandbox.makedirs('/nas/mnt/usb/下载')
            result = self.engine.add_share('fw867', target)

        self.assertEqual(target, '/nas/mnt/usb/下载')
        self.assertEqual(result['shareName'], 'fw867_nb_1')          # 命名规则没变
        argv = self.runner.argv_for('add_dir')
        self.assertEqual(argv[3], 'fw867_nb_1')
        self.assertEqual(argv[4], target)                            # 共享的是绝对路径
        self.assertEqual(argv[6], '下载')                             # 显示名 = 目录名
        self.assertTrue(result['verified'])
        self.assertIn("option path '/nas/mnt/usb/下载'",
                      self.sandbox.sambashare.read_text(encoding='utf-8'))

    def test_add_share_defaults_share_point_to_the_directory_name(self):
        """没给 share point 时用目录名（真机共享名就是 `照片-3943892` 这种显示名）。

        注意段 id 与显示名是两回事：段 id 永远是插件命名空间 `fw867_nb_1`，
        目录名只进 `option name`（smb.conf 的 section 名）。
        """
        target = self.prepare_target()
        result = self.engine.add_share('fw867', target)
        argv = self.runner.argv_for('add_dir')
        self.assertEqual(result['shareName'], 'fw867_nb_1')
        self.assertEqual(result['sharePoint'], '新照片')
        self.assertEqual(argv[3], 'fw867_nb_1')          # 段 id：add_dir 用它
        self.assertEqual(argv[6], '新照片')               # 显示名（share point）
        text = self.sandbox.sambashare.read_text(encoding='utf-8')
        self.assertIn("config sambashare 'fw867_nb_1'", text)
        self.assertIn("option name '新照片'", text)
        self.assertIn('[新照片]', self.sandbox.smb_conf.read_text(encoding='utf-8'))
        # 显示名里的空白/分隔符由 validate_share_name 挡住（与引擎实现一致）
        with self.assertRaises(Error):
            engine.validate_share_name('bad name')

    def test_add_share_never_calls_smb_mgr_reload(self):
        """回归：reload 的收尾 restart wsdd 会把失败码带出来，所以不许调用它。"""
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_dir() / '照片'
        target.mkdir()
        self.engine.add_share('fw867', self.sandbox.virtual(target))
        self.assertFalse(any('reload' in call and 'smb_mgr.sh' in call[0]
                             for call in self.runner.calls))
        self.assertIsNotNone(self.runner.argv_for('init_config'))
        self.assertIn(['/bin/systemctl', 'reload', 'smb', 'nmb'], self.runner.calls)

    def test_add_share_accepts_extra_users_and_share_point(self):
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_dir() / '照片'
        target.mkdir()
        result = self.engine.add_share('fw867', self.sandbox.virtual(target),
                                       share_point='照片-3943892',
                                       user_list='fw867 admin')
        argv = self.runner.argv_for('add_dir')
        self.assertEqual(argv[5], 'fw867 admin')
        self.assertEqual(argv[6], '照片-3943892')
        self.assertEqual(result['users'], ['fw867', 'admin'])
        share = next(item for item in self.engine.shares()
                     if item['name'] == result['shareName'])
        self.assertEqual(share['display'], '照片-3943892')

    def test_add_share_without_force_user_when_account_has_no_nas_user(self):
        self.build()
        self.manager()
        target = self.data_dir() / '共享'
        target.mkdir()
        result = self.engine.add_share('admin', self.sandbox.virtual(target))
        self.assertEqual(result['forceUser'], 'u1000001')     # 来自 sambauser 的 option user

    def test_add_share_rejects_unknown_account(self):
        self.build()
        self.manager()
        target = self.data_dir() / '照片'
        target.mkdir()
        with self.assertRaises(Error) as caught:
            self.engine.add_share('nobody', self.sandbox.virtual(target))
        self.assertIn('没有这个账号', str(caught.exception))
        self.assertIsNone(self.runner.argv_for('add_dir'))

    def test_add_share_rejects_path_outside_roots(self):
        self.build()
        self.manager()
        with self.assertRaises(Error) as caught:
            self.engine.add_share('fw867', '/etc')
        self.assertIn('不在允许的根目录内', str(caught.exception))

    def test_add_share_rejects_duplicate_directory(self):
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_dir() / '照片'
        target.mkdir()
        path = self.sandbox.virtual(target)
        self.engine.add_share('fw867', path)
        with self.assertRaises(Error) as caught:
            self.engine.add_share('fw867', path)
        self.assertIn('已经是账号', str(caught.exception))

    def test_add_share_uses_next_free_index(self):
        self.build()
        self.manager()
        self.drop_list_dirs()
        first = self.data_dir() / 'a'
        second = self.data_dir() / 'b'
        first.mkdir()
        second.mkdir()
        self.engine.add_share('fw867', self.sandbox.virtual(first))
        result = self.engine.add_share('fw867', self.sandbox.virtual(second))
        self.assertEqual(result['shareName'], 'fw867_nb_2')

    def test_add_share_allows_a_directory_listed_without_a_share_section(self):
        """`list dirs` 里有目录、但没有共享段时，插件应该能把它共享出去。

        真机语义：`sambauser` 的 `list dirs` 存的是**目录路径**，`sambashare` 才是共享。
        两者不同步时（例如共享段被删掉、只剩 dirs），`accounts()` 会把它标成 `missing`
        ——重复检查必须跳过这些条目，否则用户再也无法通过插件共享这个目录。
        """
        self.build()
        self.manager()
        target = self.data_dir() / '下载'
        target.mkdir()
        listed = next(share for share in self.engine.accounts()[0]['shares']
                      if share['missing'])
        self.assertEqual(listed['path'], '/home/u3943892/pool0/data/下载')

        result = self.engine.add_share('fw867', self.sandbox.virtual(target))

        self.assertEqual(result['shareName'], 'fw867_nb_1')
        self.assertEqual(result['path'], '/home/u3943892/pool0/data/下载')
        argv = self.runner.argv_for('add_dir')
        self.assertIsNotNone(argv)
        self.assertEqual(argv[3], 'fw867_nb_1')

    def test_add_share_reports_smb_mgr_failure_with_output(self):
        self.build(runner=FakeRunner(answers={'add_dir': (1, '', 'share exists\n')}))
        self.drop_list_dirs()
        target = self.data_dir() / '照片'
        target.mkdir()
        with self.assertRaises(Error) as caught:
            self.engine.add_share('fw867', self.sandbox.virtual(target))
        text = str(caught.exception)
        self.assertIn('共享名已存在', text)
        self.assertIn('share exists', text)                 # stderr 原样回显
        self.assertIn('exit code: 1', text)

    def test_add_share_fails_when_config_never_shows_the_share(self):
        """smb_mgr 成功但配置里没有：要报错而不是假装成功。"""
        self.build(runner=FakeRunner(answers={'add_dir': (0, 'ok', '')}))
        self.drop_list_dirs()
        target = self.data_dir() / '照片'
        target.mkdir()
        with patch.object(self.engine, 'smb_service_active', return_value=False):
            with self.assertRaises(Error) as caught:
                self.engine.add_share('fw867', self.sandbox.virtual(target))
        self.assertIn('没有出现在 /var/etc/smb.conf', str(caught.exception))

    def test_reload_failure_is_reported_but_not_fatal(self):
        self.build()
        runner = self.manager()
        self.drop_list_dirs()
        target = self.data_dir() / '照片'
        target.mkdir()
        original = runner.handler

        def handler(inner, key, timeout):
            if 'reload' in key:
                inner.calls.append(key)
                return Result(1, '', 'Failed to reload smb.service')
            return original(inner, key, timeout)

        runner.handler = handler
        result = self.engine.add_share('fw867', self.sandbox.virtual(target))
        self.assertTrue(result['verified'])
        self.assertEqual(result['reloadReturncode'], 1)
        self.assertTrue(any('重新加载 Samba 服务返回非 0' in item for item in result['errors']))

    def test_takeover_dropin_does_not_break_add(self):
        """官方 wsdd 被 drop-in 变空操作时，add_dir 仍要走完整条链路并成功。"""
        self.build()
        self.manager()
        self.drop_list_dirs()
        self.engine.start()
        self.assertTrue(self.engine.discovery['dropin'])
        target = self.data_dir() / '照片'
        target.mkdir()
        result = self.engine.add_share('fw867', self.sandbox.virtual(target))
        self.assertTrue(result['verified'])


class ApplySambaBatchDirectionTests(EngineHarness):
    """`apply_samba_batch(expect_present=...)` 的两种判定方向。"""

    def test_default_direction_means_present(self):
        """默认形参必须与批量新增路径完全一致：配置里有段才算 verified。"""
        self.build()
        self.manager()
        applied = self.engine.apply_samba_batch(['u3943892_nb_1'])
        self.assertEqual(applied['shares']['u3943892_nb_1'],
                         {'present': True, 'verified': True})
        self.assertTrue(applied['inConfig'])

    def test_expect_present_false_verifies_disappearance(self):
        """删除路径：配置里**没有**该段才算 verified，inConfig 表示「全都不在配置里」。"""
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_child('下载')
        self.engine.add_share('fw867', target)
        self.engine.shares(force=True)
        # 先真的把段删掉（这里只测 `apply_samba_batch` 的判定方向，不测 del_dir）
        self.engine.exec([self.sandbox.smb_mgr, 'shares', 'del_dir', 'fw867_nb_1'])

        applied = self.engine.apply_samba_batch(['fw867_nb_1'], expect_present=False)
        self.assertTrue(applied['shares']['fw867_nb_1']['verified'])
        self.assertFalse(applied['shares']['fw867_nb_1']['present'])
        self.assertTrue(applied['inConfig'])          # 段确实不在了
        # 删除路径不去问 is-active
        self.assertFalse(applied['smbActive'])
        self.assertIsNone(self.runner.argv_for('is-active'))

    def test_expect_present_false_reports_still_present(self):
        """段还在配置里：verified=False，inConfig=False（但函数本身不抛错）。"""
        self.build()
        self.manager()
        applied = self.engine.apply_samba_batch(['u3943892_nb_1'], expect_present=False)
        entry = applied['shares']['u3943892_nb_1']
        self.assertFalse(entry['verified'])
        self.assertTrue(entry['present'])
        self.assertFalse(applied['inConfig'])


class DeleteSharesBatchTests(EngineHarness):
    """批量删除：`del_dir` × N → 只跑一次 init_config + reload，先全部校验后动手。"""

    def prepare(self):
        self.build()
        runner = self.manager()
        self.drop_list_dirs()
        targets = [self.data_child(name) for name in ('照片', '视频', '音乐')]
        self.engine.add_shares('fw867', targets)
        self.engine.shares(force=True)
        return runner

    def test_deletes_all_names_with_one_reload(self):
        runner = self.prepare()
        runner.calls.clear()
        result = self.engine.delete_shares(['fw867_nb_1', 'fw867_nb_2'])

        self.assertEqual([item['shareName'] for item in result['removed']],
                         ['fw867_nb_1', 'fw867_nb_2'])
        self.assertEqual([item['account'] for item in result['removed']],
                         ['fw867', 'fw867'])
        self.assertTrue(all(item['verified'] for item in result['removed']))
        self.assertFalse(any(item['inConfig'] for item in result['removed']))
        self.assertTrue(result['verified'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['reloadReturncode'], 0)
        self.assertEqual(runner.count('del_dir'), 2)
        self.assertIsNone(runner.argv_for('del_dir', 2))          # 没有第三条
        self.assertEqual(runner.count('init_config'), 1)
        self.assertEqual(
            runner.calls.count(['/bin/systemctl', 'reload', 'smb', 'nmb']), 1)
        # 收尾命令只跑一次，而且必须排在所有 del_dir 之后
        last_del = max(index for index, call in enumerate(runner.calls) if 'del_dir' in call)
        init_at = runner.calls.index([self.sandbox.smb_mgr, 'init_config'])
        reload_at = runner.calls.index(['/bin/systemctl', 'reload', 'smb', 'nmb'])
        self.assertLess(last_del, init_at)
        self.assertLess(init_at, reload_at)
        # 配置与缓存都刷新了
        self.assertNotIn('fw867_nb_1', [item['name'] for item in self.engine.shares()])
        self.assertNotIn('fw867_nb_2', [item['name'] for item in self.engine.shares()])
        self.assertIn('fw867_nb_3', [item['name'] for item in self.engine.shares()])

    def test_deduplicates_and_ignores_blanks(self):
        runner = self.prepare()
        runner.calls.clear()
        result = self.engine.delete_shares(['fw867_nb_1', '', '  ', 'fw867_nb_1', None])
        self.assertEqual([item['shareName'] for item in result['removed']], ['fw867_nb_1'])
        self.assertEqual(runner.count('del_dir'), 1)
        self.assertEqual(runner.count('init_config'), 1)

    def test_empty_list_is_rejected_without_commands(self):
        runner = self.prepare()
        runner.calls.clear()
        for value in ([], ['', '  '], None):
            with self.subTest(value=value):
                with self.assertRaises(Error) as caught:
                    self.engine.delete_shares(value)
                self.assertIn('请选择要删除的共享', str(caught.exception))
        self.assertIsNone(runner.argv_for('del_dir'))
        self.assertIsNone(runner.argv_for('init_config'))

    def test_protected_share_aborts_everything(self):
        """列表里有受保护的共享：一条命令都不许跑（连合法的也不能先删）。"""
        runner = self.prepare()
        runner.calls.clear()
        with self.assertRaises(Error) as caught:
            self.engine.delete_shares(['fw867_nb_1', 'public'])
        self.assertIn('不允许删除', str(caught.exception))
        self.assertIsNone(runner.argv_for('del_dir'))
        self.assertIsNone(runner.argv_for('init_config'))
        self.assertIn('fw867_nb_1', [item['name'] for item in self.engine.shares()])

    def test_unknown_and_app_managed_shares_abort_everything(self):
        runner = self.prepare()
        runner.calls.clear()
        with self.assertRaises(Error) as caught:
            self.engine.delete_shares(['fw867_nb_1', 'does_not_exist'])
        self.assertIn('没有找到共享', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.delete_shares(['fw867_nb_1', 'u3943892_nb_1'])
        self.assertIn('只允许删除插件自己添加的共享', str(caught.exception))
        self.assertIsNone(runner.argv_for('del_dir'))
        self.assertIsNone(runner.argv_for('init_config'))

    def test_reports_when_config_still_has_the_share(self):
        """del_dir 成功但配置没变：如实报出来，不假装删掉了。"""
        runner = self.prepare()
        runner.calls.clear()

        def handler(inner, key, timeout):
            if 'del_dir' in key:
                inner.calls.append(key)
                return Result(0, '', '')               # 什么都不删
            return None

        runner.handler = handler
        result = self.engine.delete_shares(['fw867_nb_1'])
        self.assertEqual(result['removed'][0]['verified'], False)
        self.assertEqual(result['removed'][0]['inConfig'], True)
        self.assertFalse(result['verified'])
        self.assertTrue(any('删除未生效' in item for item in result['errors']))
        self.assertEqual(runner.count('init_config'), 1)      # 收尾还是只跑一次


class DeleteShareTests(EngineHarness):
    def prepare(self):
        self.build()
        runner = self.manager()
        first = self.data_dir() / '照片'
        second = self.data_dir() / '视频'
        first.mkdir()
        second.mkdir()
        self.engine.add_share('fw867', self.sandbox.virtual(first))
        self.engine.add_share('fw867', self.sandbox.virtual(second))
        return runner, first, second

    def test_delete_plugin_share_calls_del_dir(self):
        runner, _first, _second = self.prepare()
        names = [item['name'] for item in self.engine.shares()]
        self.assertIn('fw867_nb_1', names)
        result = self.engine.delete_share('fw867_nb_1')
        self.assertEqual(runner.argv_for('del_dir'),
                         [self.sandbox.smb_mgr, 'shares', 'del_dir', 'fw867_nb_1'])
        self.assertNotIn('fw867_nb_1', [item['name'] for item in self.engine.shares()])
        self.assertTrue(result['verified'])

    def test_delete_refuses_app_generated_share(self):
        """App 生成的共享（段 id 是 `u3943892_nb_1` 这种 NAS 用户命名空间）不许删。"""
        self.build()
        runner = self.manager()
        with self.assertRaises(Error) as caught:
            self.engine.delete_share('u3943892_nb_1')
        self.assertIn('只允许删除插件自己添加的共享', str(caught.exception))
        self.assertIsNone(runner.argv_for('del_dir'))

    def test_delete_refuses_protected_and_unknown(self):
        self.build()
        runner = self.manager()
        with self.assertRaises(Error) as caught:
            self.engine.delete_share('public')
        self.assertIn('不允许删除', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.delete_share('does_not_exist')
        self.assertIn('没有找到共享', str(caught.exception))
        self.assertIsNone(runner.argv_for('del_dir'))

    def test_delete_needs_a_name(self):
        self.build()
        self.manager()
        with self.assertRaises(Error):
            self.engine.delete_share('')

    def test_delete_noop_config_change_is_verified(self):
        """del_dir 成功但配置没变：要报错。"""
        runner, _first, _second = self.prepare()

        def handler(inner, key, timeout):
            if 'del_dir' in key:
                inner.calls.append(key)
                return Result(0, '', '')          # 什么都不删
            return None

        runner.handler = handler
        with self.assertRaises(Error) as caught:
            self.engine.delete_share('fw867_nb_1')
        self.assertIn('删除未生效', str(caught.exception))


class StatusTests(EngineHarness):
    def test_status_shape(self):
        self.build()
        self.manager()
        self.engine.start()
        self.engine.on_responder_log('wsd http: POST /x from 192.168.1.5 action=Get')
        data = self.engine.status()
        self.assertTrue(data['ok'])
        self.assertEqual(data['version'], engine.VERSION)
        discovery = data['discovery']
        self.assertTrue(discovery['running'])
        self.assertEqual(discovery['hostname'], 'SmartStorage')     # 来自 /etc/config/samba
        self.assertEqual(discovery['workgroup'], 'WORKGROUP')
        self.assertEqual(discovery['xaddrs'],
                         ['http://192.168.1.8:15357/11111111-2222-3333-4444-555555555555'])
        self.assertTrue(discovery['managed'])
        self.assertTrue(discovery['wsddDropin'])
        self.assertEqual(discovery['wsddUnit'], 'wsdd')
        self.assertIn('netneighbor.conf', discovery['wsddDropinPath'])
        self.assertEqual(discovery['metadataPosts'], 1)
        self.assertEqual(len(data['accounts']), 2)
        self.assertIn('/home/u3943892/pool0/data', data['allowedRoots'])
        self.assertIn('/nas/pool0', data['allowedRoots'])

    def test_identity_defaults_come_from_samba_config(self):
        self.build()
        engine.SAMBA_CONFIG.write_text(
            "config samba 'global'\n\toption name 'MyNAS'\n\toption workgroup 'HOME'\n",
            encoding='utf-8')
        self.assertEqual(engine.samba_identity(), ('MyNAS', 'HOME'))

    def test_recent_log_is_capped(self):
        self.build()
        self.manager()
        for index in range(120):
            self.engine.log('line %d' % index)
        lines = self.engine.recent_log(10)
        self.assertEqual(len(lines), 10)
        self.assertIn('line 119', lines[-1])


class WsdResponderStartFailureTests(unittest.TestCase):
    """`wsd.WsdResponder.start()` 的失败清理：先绑 UDP 3702 再起元数据 HTTP。

    全部用假 socket，离线可跑；只验证真实实现里的「半启动状态必须收拾干净」。
    """

    class _FakeSocket:
        def __init__(self, fail_bind=False):
            self.fail_bind = fail_bind
            self.closed = False
            self.bound_to = None

        def setsockopt(self, *_args, **_kwargs):
            return None

        def bind(self, address):
            self.bound_to = address
            if self.fail_bind:
                raise OSError(98, 'Address already in use')

        def settimeout(self, _value):
            return None

        def sendto(self, *_args, **_kwargs):
            return None

        def close(self):
            self.closed = True

        def recvfrom(self, _size):
            raise OSError('已关闭')

    def _patches(self, sockets, failures, servers):
        """按顺序弹出假 socket / HTTP 服务，并记录失败次数。"""
        created = list(sockets)
        results = list(failures)

        def factory(*_args, **_kwargs):
            return created.pop(0)

        class FakeHTTP:
            def __init__(self, *_args, **_kwargs):
                servers.append(self)
                self.daemon_threads = False
                self.shutdown_called = False
                self.server_close_called = False

            def serve_forever(self):
                return None

            def shutdown(self):
                self.shutdown_called = True

            def server_close(self):
                self.server_close_called = True

        def make_server(*args, **kwargs):
            if results:
                error = results.pop(0)
                if error is not None:
                    raise error
            return FakeHTTP(*args, **kwargs)

        return factory, make_server, FakeHTTP

    def test_udp_bind_failure_releases_everything(self):
        udp = self._FakeSocket(fail_bind=True)
        servers: list = []
        factory, make_server, _fake_http = self._patches([udp], [None], servers)
        responder = wsd.WsdResponder(hostname='NAS', workgroup='WG',
                                     address='192.168.1.8', port=5357, state_file='')
        with patch.object(wsd.socket, 'socket', factory), \
                patch.object(wsd, 'ThreadingHTTPServer', make_server):
            with self.assertRaises(OSError) as caught:
                responder.start()
        self.assertIn('Address already in use', str(caught.exception))
        self.assertEqual(udp.bound_to, ('0.0.0.0', wsd.WSD_PORT))   # 先绑的就是 3702
        self.assertTrue(udp.closed)                                 # 半启动状态已释放
        self.assertIsNone(responder.http)                           # 5357 从未被占用
        self.assertEqual(servers, [])                               # 连实例都没建
        self.assertEqual(responder.threads, [])                     # 没有留下线程

    def test_http_bind_failure_releases_udp_3702(self):
        """实测坑：UDP 成功但 5357 被占时，如果不清理，UDP 3702 会一直被自己占着。"""
        udp = self._FakeSocket()
        servers: list = []
        factory, make_server, _fake_http = self._patches(
            [udp], [OSError(98, 'Address already in use')], servers)
        responder = wsd.WsdResponder(hostname='NAS', workgroup='WG',
                                     address='192.168.1.8', port=5357, state_file='')
        with patch.object(wsd.socket, 'socket', factory), \
                patch.object(wsd, 'ThreadingHTTPServer', make_server):
            with self.assertRaises(OSError):
                responder.start()
        self.assertIsNone(responder.http)
        self.assertTrue(udp.closed)
        self.assertTrue(responder.stopping.is_set())                # stop() 真的跑过
        # 监听线程由 stop() 关掉 socket 后自行退出，实例上不留半启动状态
        self.assertTrue(all(item.daemon for item in responder.threads))

    def test_start_orders_udp_before_http_and_succeeds(self):
        udp = self._FakeSocket()
        servers: list = []
        factory, make_server, _fake_http = self._patches([udp], [None], servers)
        responder = wsd.WsdResponder(hostname='NAS', workgroup='WG',
                                     address='192.168.1.8', port=5357, state_file='')
        with patch.object(wsd.socket, 'socket', factory), \
                patch.object(wsd, 'ThreadingHTTPServer', make_server), \
                patch.object(responder, 'announce_hello', return_value=True):
            responder.start()
            responder.stop()
        self.assertEqual(udp.bound_to, ('0.0.0.0', wsd.WSD_PORT))
        self.assertEqual(len(servers), 1)                           # UDP 成功后才起 HTTP
        self.assertTrue(servers[0].shutdown_called)
        self.assertTrue(servers[0].server_close_called)
        self.assertTrue(udp.closed)


class SambaIdentityAndHostnameTests(unittest.TestCase):
    """主机名校验与生效优先级（纯函数，不需要沙箱）。"""

    def test_accepts_netbios_friendly_names(self):
        for good in ['NAS', 'SmartStorage', 'my-nas', 'nas_1.example', 'a' * 15, 'fw867']:
            with self.subTest(good=good):
                self.assertTrue(engine.hostname_valid(good))
                self.assertEqual(engine.validate_hostname(good), good)
                self.assertEqual(engine.hostname_error(good), '')

    def test_rejects_bad_names_with_chinese_reasons(self):
        cases = {
            '': '请填写主机名',
            '   ': '请填写主机名',
            'a' * 16: '最长 15 个字符',
            '-nas': '字母或数字开头',
            '.nas': '字母或数字开头',
            'nas 01': '只能包含',
            'nas/01': '只能包含',
            '存储': '字母或数字开头',
        }
        for bad, needle in cases.items():
            with self.subTest(bad=bad):
                self.assertFalse(engine.hostname_valid(bad))
                self.assertIn(needle, engine.hostname_error(bad))
                with self.assertRaises(Error) as caught:
                    engine.validate_hostname(bad)
                self.assertIn(needle, str(caught.exception))

    def test_resolve_hostname_ignores_any_stored_settings(self):
        """插件不再接受/不再存用户提供的名字：宣告名只跟 `/etc/config/samba` 走。"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.addCleanup(sandbox.close)
            samba = sandbox.root / 'etc' / 'config' / 'samba'
            engine_obj = sandbox.engine
            # 1) 用 /etc/config/samba 里的名字（读不到才是内置默认值）
            self.assertEqual(engine_obj._resolve_hostname(), 'SmartStorage')
            samba.write_text("config samba 'global'\n\toption name 'MyNAS'\n", encoding='utf-8')
            self.assertEqual(engine_obj._resolve_hostname(), 'MyNAS')
            # 2) 旧版本落盘的 hostname 一律忽略，也不会写回设置里
            engine.save_settings({**engine_obj.settings, 'hostname': 'Custom1'},
                                 sandbox.data / 'settings.json')
            reloaded = engine.load_settings(sandbox.data / 'settings.json')
            self.assertNotIn('hostname', reloaded)
            self.assertEqual(engine_obj._resolve_hostname(), 'MyNAS')


class DiscoveryToggleTests(EngineHarness):
    """「网络发现」开关：落盘、重启后生效、关闭幂等。"""

    def test_settings_default_to_enabled(self):
        self.build()
        data = self.engine.status()
        # 设置里只剩「网络发现」开关：主机名已改成只读展示（identity 里自动获取）
        self.assertEqual(data['settings'], {'discoveryEnabled': True})
        self.assertTrue(data['discovery']['enabled'])
        self.assertTrue(self.engine.discovery['enabled'])

    def test_disabled_is_persisted_and_does_not_take_over_on_restart(self):
        self.build()
        self.manager()
        self.engine.start()
        self.assertTrue(self.engine.discovery['dropin'])
        self.assertTrue(self.engine.discovery['running'])

        snapshot = self.engine.set_discovery_enabled(False)
        self.assertFalse(snapshot['settings']['discoveryEnabled'])
        self.assertFalse(snapshot['discovery']['running'])
        self.assertFalse(snapshot['discovery']['managed'])
        self.assertFalse(snapshot['discovery']['wsddDropin'])
        self.assertFalse(self.sandbox.dropin.exists())          # drop-in 被删掉
        self.assertIsNone(self.engine.responder)

        # 落盘：settings.json 里 discoveryEnabled = false，且不再有 hostname 键
        saved = json.loads((self.sandbox.data / 'settings.json').read_text(encoding='utf-8'))
        self.assertIs(saved['discoveryEnabled'], False)
        self.assertNotIn('hostname', saved)

        # 重启（新 Engine 走同一个 DATA_DIR / 同一个 runner）：关闭状态不自动接管
        self.runner.calls.clear()
        restarted = Engine(data_dir=self.sandbox.data, runner=(lambda: self.runner),
                           responder_factory=FakeResponder, samba_mgr=self.sandbox.smb_mgr,
                           systemctl='/bin/systemctl', start_responder=False)
        restarted.start()
        self.assertFalse(restarted.discovery['running'])
        self.assertFalse(restarted.discovery['enabled'])
        self.assertFalse(restarted.discovery['managed'])
        self.assertIsNone(restarted.responder)
        self.assertFalse(self.sandbox.dropin.exists())
        self.assertFalse(any('mask' in call for call in self.runner.calls))
        self.assertFalse(any('stop' in call for call in self.runner.calls))

    def test_disabled_is_idempotent_even_when_nothing_was_managed(self):
        """关闭时：官方 wsdd 本来就在跑、drop-in 不存在，也要成功返回。"""
        self.build()
        self.manager()
        self.engine.set_discovery_enabled(False)                # 从没接管过就关闭
        self.engine.set_discovery_enabled(False)                # 再来一次
        starts = [call for call in self.runner.calls
                  if call[:3] == ['/bin/systemctl', 'start', 'wsdd']]
        self.assertEqual(len(starts), 2)                        # 每次都是幂等的 start
        self.assertFalse(self.sandbox.dropin.exists())
        self.assertFalse(self.engine.discovery['running'])
        self.assertIn('没有需要清理的 drop-in',
                      '\n'.join(self.engine.recent_log(50)))

    def test_disabled_restores_official_wsdd(self):
        self.build()
        self.manager()
        self.engine.start()
        self.runner.calls.clear()
        self.engine.set_discovery_enabled(False)
        self.assertIn(['/bin/systemctl', 'daemon-reload'], self.runner.calls)
        self.assertIn(['/bin/systemctl', 'start', 'wsdd'], self.runner.calls)

    def test_disabled_responder_is_not_started_and_wsdd_is_restored(self):
        self.build(runner=FakeRunner(answers={
            '/bin/systemctl stop wsdd': (0, '', ''), 'killall': (0, '', '')}))
        self.engine.set_discovery_enabled(True)
        self.assertTrue(self.engine.discovery['running'])
        self.runner.calls.clear()
        snapshot = self.engine.set_discovery_enabled('off')      # 字符串也要认
        self.assertFalse(snapshot['discovery']['running'])
        self.assertEqual(FakeResponder.instances[-1].stopped, True)
        self.assertEqual(self.engine.responder, None)
        self.assertIn(['/bin/systemctl', 'start', 'wsdd'], self.runner.calls)

    def test_reenabling_takes_over_again(self):
        self.build()
        self.manager()
        self.engine.set_discovery_enabled(False)
        self.engine.set_discovery_enabled(True)
        self.assertTrue(self.engine.discovery['enabled'])
        self.assertTrue(self.engine.discovery['running'])
        self.assertTrue(self.engine.discovery['dropin'])
        self.assertTrue(self.sandbox.dropin.exists())
        self.assertTrue(self.engine.responder.started)


class SystemHostnameTests(unittest.TestCase):
    """身份自动获取的纯函数：系统主机名 / SMB 名 / IP / 访问提示 / uci 文本改写。"""

    def test_system_hostname_prefers_uci_then_command_then_samba(self):
        """优先级：uci system 主机名 → 系统调用 hostname → samba 的 option name。"""
        with patch.object(engine, 'uci_option', lambda name: 'nas-uci' if name else ''), \
                patch.object(engine, 'path_gethostname', lambda: 'minasc71ab1'), \
                patch.object(engine, 'samba_config_value', lambda option, default='': 'SmartStorage'):
            self.assertEqual(engine.resolve_system_hostname(), 'nas-uci')
        with patch.object(engine, 'uci_option', lambda name: ''), \
                patch.object(engine, 'path_gethostname', lambda: 'minasc71ab1'), \
                patch.object(engine, 'samba_config_value', lambda option, default='': 'SmartStorage'):
            self.assertEqual(engine.resolve_system_hostname(), 'minasc71ab1')
        with patch.object(engine, 'uci_option', lambda name: ''), \
                patch.object(engine, 'path_gethostname', lambda: ''), \
                patch.object(engine, 'samba_config_value', lambda option, default='': 'SmartStorage'):
            self.assertEqual(engine.resolve_system_hostname(), 'SmartStorage')
        with patch.object(engine, 'uci_option', lambda name: ''), \
                patch.object(engine, 'path_gethostname', lambda: ''), \
                patch.object(engine, 'samba_config_value', lambda option, default='': ''):
            self.assertEqual(engine.resolve_system_hostname(), engine.DEFAULT_HOSTNAME)

    def test_netbios_name_is_read_from_smb_conf(self):
        text = ("[global]\n\tworkgroup = WORKGROUP\n\tnetbios name = minasc71ab1\n"
                "\tnetbios name = ignored\n[share]\n")
        self.assertEqual(engine.smb_conf_netbios_name(text, 'fallback'), 'minasc71ab1')
        self.assertEqual(engine.smb_conf_netbios_name('NetBIOS Name = "Quoted"\n', 'x'), 'Quoted')
        # 读不到就回退（samba 配置里的 option name）
        self.assertEqual(engine.smb_conf_netbios_name('[global]\n', 'SmartStorage'), 'SmartStorage')
        self.assertEqual(engine.smb_conf_netbios_name('', ''), '')

    def test_parse_ip_route_src(self):
        self.assertEqual(engine.parse_ip_route_src(
            '223.5.5.5 via 192.168.1.1 dev eth0 src 192.168.1.8 uid 0\n'), '192.168.1.8')
        self.assertEqual(engine.parse_ip_route_src('RTNETLINK answers: Network is unreachable'),
                         '')

    def test_access_hint_uses_the_auto_values(self):
        """提示用 SMB 名 + IP 现拼（两个值都自动获取），**不带**括号里的说明。"""
        self.assertEqual(engine.access_hint('minasc71ab1', '192.168.1.8'),
                         'Windows 里用 \\\\minasc71ab1 或 \\\\192.168.1.8 访问')
        self.assertNotIn('（', engine.access_hint('minasc71ab1', '192.168.1.8'))
        # 没有 IP 时只给名字那一半
        self.assertEqual(engine.access_hint('minasc71ab1', ''),
                         'Windows 里用 \\\\minasc71ab1 访问')
        # 名字都拿不到：不显示提示（页面拿到空串就不渲染）
        self.assertEqual(engine.access_hint('', '192.168.1.8'), '')
        self.assertEqual(engine.access_hint('', ''), '')

    def test_set_uci_option_replaces_inserts_and_appends(self):
        text = ("config samba 'global'\n\toption workgroup 'WORKGROUP'\n"
                "\toption name 'SmartStorage'\n\nconfig sambashare 'x'\n\toption path '/a'\n")
        updated = engine.set_uci_option(text, 'samba', 'name', 'minasc71ab1')
        self.assertIn("option name 'minasc71ab1'", updated)
        self.assertNotIn('SmartStorage', updated)
        self.assertIn("option workgroup 'WORKGROUP'", updated)     # 其它行原样保留
        self.assertIn("config sambashare 'x'", updated)
        self.assertIn("option path '/a'", updated)                 # 别的 section 没被动
        # option 不存在：插在 section 头之后
        inserted = engine.set_uci_option("config samba 'global'\n\toption workgroup 'W'\n",
                                         'samba', 'name', 'nas1')
        self.assertIn("\toption name 'nas1'", inserted)
        self.assertLess(inserted.index('config samba'), inserted.index("option name 'nas1'"))
        # section 不存在：追加一段
        appended = engine.set_uci_option("config foo 'bar'\n", 'samba', 'name', 'nas1')
        self.assertIn("config samba\n\toption name 'nas1'", appended)
        self.assertIn("config foo 'bar'", appended)


class IdentitySnapshotTests(EngineHarness):
    """页面只读展示的身份：值必须来自**自动获取**，不能写死。"""

    def test_matched_names_have_no_warning(self):
        self.build()
        self.sandbox.system_hostname = 'minasc71ab1'
        self.sandbox.lan_ip = '192.168.1.8'
        (self.root / 'etc' / 'config' / 'samba').write_text(
            "config samba 'global'\n\toption name 'minasc71ab1'\n", encoding='utf-8')
        self.sandbox.smb_conf.write_text('[global]\n\tnetbios name = minasc71ab1\n',
                                        encoding='utf-8')

        identity = self.engine.identity_snapshot()

        self.assertEqual(identity['systemName'], 'minasc71ab1')      # 来自桩，不是写死的
        self.assertEqual(identity['netbiosName'], 'minasc71ab1')
        self.assertTrue(identity['matched'])
        self.assertEqual(identity['warning'], '')
        self.assertEqual(identity['address'], '192.168.1.8')
        self.assertIn('\\\\minasc71ab1', identity['hint'])
        self.assertIn('\\\\192.168.1.8', identity['hint'])

    def test_mismatch_reports_warning_and_hint_uses_the_smb_name(self):
        self.build()
        self.sandbox.system_hostname = 'minasc71ab1'
        self.sandbox.lan_ip = '10.0.0.5'
        # /etc/config/samba 还是旧名字（真机上就是这个状态：Windows 连不上）
        (self.root / 'etc' / 'config' / 'samba').write_text(
            "config samba 'global'\n\toption name 'SmartStorage'\n", encoding='utf-8')
        self.sandbox.smb_conf.write_text('[global]\n\tnetbios name = SmartStorage\n',
                                        encoding='utf-8')

        identity = self.engine.identity_snapshot()

        # 系统主机名只在服务端当判据（页面不展示它）
        self.assertEqual(identity['systemName'], 'minasc71ab1')
        self.assertEqual(identity['netbiosName'], 'SmartStorage')
        self.assertFalse(identity['matched'])
        self.assertIn('不一致', identity['warning'])
        self.assertIn('系统主机名', identity['warning'])
        self.assertIn('恢复为系统主机名', identity['warning'])
        # 提示里用的是**页面上展示的那个名字**（SMB 名）+ IP
        self.assertEqual(identity['hint'], 'Windows 里用 \\\\SmartStorage 或 \\\\10.0.0.5 访问')
        self.assertNotIn('minasc71ab1', identity['hint'])
        self.assertNotIn('（', identity['hint'])                    # 括号说明已删掉

    def test_netbios_falls_back_to_the_samba_option_name(self):
        self.build()
        self.sandbox.system_hostname = 'minasc71ab1'
        (self.root / 'etc' / 'config' / 'samba').write_text(
            "config samba 'global'\n\toption name 'minasc71ab1'\n", encoding='utf-8')
        self.sandbox.smb_conf.write_text('[global]\n', encoding='utf-8')   # 没有 netbios name

        identity = self.engine.identity_snapshot()
        self.assertEqual(identity['netbiosName'], 'minasc71ab1')
        self.assertTrue(identity['matched'])

    def test_hint_without_an_address(self):
        self.build()
        self.sandbox.system_hostname = 'minasc71ab1'
        self.sandbox.lan_ip = ''
        (self.root / 'etc' / 'config' / 'samba').write_text(
            "config samba 'global'\n\toption name 'minasc71ab1'\n", encoding='utf-8')
        identity = self.engine.identity_snapshot()
        self.assertEqual(identity['address'], '')
        self.assertEqual(identity['hint'], 'Windows 里用 \\\\minasc71ab1 访问')
        self.assertNotIn('或 \\\\', identity['hint'])

    def test_status_exposes_identity_for_the_page(self):
        self.build()
        self.sandbox.system_hostname = 'minasc71ab1'
        self.sandbox.lan_ip = '192.168.1.8'
        (self.root / 'etc' / 'config' / 'samba').write_text(
            "config samba 'global'\n\toption name 'minasc71ab1'\n", encoding='utf-8')
        data = self.engine.status()
        self.assertEqual(data['identity']['systemName'], 'minasc71ab1')   # 内部判据仍在
        self.assertEqual(data['identity']['netbiosName'], 'minasc71ab1')
        self.assertEqual(data['identity']['address'], '192.168.1.8')
        self.assertEqual(data['identity']['hint'],
                         'Windows 里用 \\\\minasc71ab1 或 \\\\192.168.1.8 访问')


class RestoreSmbNameTests(EngineHarness):
    """「恢复为系统主机名」：写 option name → init_config → restart 三个服务 → 回读校验。"""

    def prepare(self, system_name='minasc71ab1', samba_name='SmartStorage'):
        self.build()
        self.manager()
        self.sandbox.system_hostname = system_name
        (self.root / 'etc' / 'config' / 'samba').write_text(
            "config samba 'global'\n\toption name '%s'\n" % samba_name, encoding='utf-8')
        self.sandbox.smb_conf.write_text('[global]\n\tnetbios name = %s\n' % samba_name,
                                        encoding='utf-8')
        return self.engine

    def test_restore_writes_the_system_name_then_restarts_and_verifies(self):
        engine_obj = self.prepare()
        result = engine_obj.restore_smb_hostname()

        self.assertEqual(result['name'], 'minasc71ab1')
        self.assertTrue(result['verified'])
        self.assertEqual(result['netbiosName'], 'minasc71ab1')
        self.assertEqual(result['units'], ['smb', 'nmb', 'wsdd'])
        self.assertEqual(result['errors'], [])
        # 写的是系统主机名（用户输入的名字一律不写）
        samba = (self.root / 'etc' / 'config' / 'samba').read_text(encoding='utf-8')
        self.assertIn("option name 'minasc71ab1'", samba)
        self.assertNotIn('SmartStorage', samba)
        # 调用顺序：init_config → restart smb nmb wsdd（不是 reload）
        calls = [call for call in self.runner.calls if 'init_config' in call or 'restart' in call]
        self.assertIn('init_config', calls[0])
        self.assertEqual(calls[1], ['/bin/systemctl', 'restart', 'smb', 'nmb', 'wsdd'])
        self.assertFalse(any('reload' in call for call in self.runner.calls))
        # 回读的是 smb.conf（真脚本由 option name 生成 netbios name）
        self.assertEqual(engine_obj.smb_netbios_name(), 'minasc71ab1')

    def test_restore_reports_when_the_readback_still_shows_the_old_name(self):
        engine_obj = self.prepare()
        original = self.runner.handler

        def handler(runner, key, timeout):
            if 'init_config' in key:
                # 故意不更新 smb.conf 的 netbios name：回读校验必须如实报失败
                return Result(0, 'ok\n', '')
            return original(runner, key, timeout)

        self.runner.handler = handler
        result = engine_obj.restore_smb_hostname()

        self.assertFalse(result['verified'])
        self.assertEqual(result['netbiosName'], 'SmartStorage')
        self.assertTrue(any('netbios name' in item for item in result['errors']))
        self.assertIn('init_config', result['output'])          # 原始命令输出也带回去

    def test_restore_restarts_even_when_the_config_already_matches(self):
        """配置里名字对、但 smb.conf 还是旧的：照样 init_config + restart 修好它。"""
        engine_obj = self.prepare(samba_name='minasc71ab1')
        result = engine_obj.restore_smb_hostname()
        self.assertTrue(result['verified'])
        self.assertEqual(self.runner.count('init_config'), 1)
        self.assertEqual(
            self.runner.calls.count(['/bin/systemctl', 'restart', 'smb', 'nmb', 'wsdd']), 1)

    def test_restore_rejects_a_system_name_that_cannot_be_an_smb_name(self):
        engine_obj = self.prepare(system_name='bad name')
        with self.assertRaises(Error) as caught:
            engine_obj.restore_smb_hostname()
        self.assertIn('不能作为 SMB 名', str(caught.exception))
        # 什么都没动：没有命令、配置没被改写
        self.assertIsNone(self.runner.argv_for('init_config'))
        self.assertFalse(any('restart' in call for call in self.runner.calls))
        self.assertIn('SmartStorage',
                      (self.root / 'etc' / 'config' / 'samba').read_text(encoding='utf-8'))

    def test_restore_rebuilds_a_running_responder_with_the_system_name(self):
        engine_obj = self.prepare()
        engine_obj.start()                                  # 接管 + 起回应器
        first = engine_obj.responder
        self.assertEqual(first.hostname, 'SmartStorage')    # 接管时用的是 samba 旧名字

        result = engine_obj.restore_smb_hostname()

        self.assertTrue(result['verified'])
        self.assertTrue(first.stopped)                      # 旧的先发 Bye
        self.assertEqual(engine_obj.responder.hostname, 'minasc71ab1')
        self.assertTrue(engine_obj.discovery['running'])

    def test_restore_does_not_touch_a_stopped_responder(self):
        engine_obj = self.prepare()
        engine_obj.set_discovery_enabled(False)
        result = engine_obj.restore_smb_hostname()
        self.assertTrue(result['verified'])
        self.assertIsNone(engine_obj.responder)             # 关闭状态不悄悄起回应器


class AccountDataRootTests(EngineHarness):
    """数据根目录的推导：账号 → sambauser 的 option user → /home/<uXXXX>/pool0/data。"""

    def test_root_from_sambauser_user(self):
        self.build()
        self.data_dir()
        self.assertEqual(self.engine.account_data_root('fw867'), NAS_DATA_ROOT)
        self.assertEqual(self.engine.account_data_root('admin'),
                         '/home/u1000001/pool0/data')

    def test_unknown_account_and_empty_account(self):
        self.build()
        with self.assertRaises(Error) as caught:
            self.engine.account_data_root('nobody')
        self.assertIn('没有这个账号', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.account_data_root('')
        self.assertIn('请选择账号', str(caught.exception))

    def test_falls_back_to_first_home_root_without_nas_user(self):
        self.build()
        self.sandbox.samba_user.write_text(
            "config sambauser 'x_1'\n\toption name 'fw867'\n", encoding='utf-8')
        self.assertEqual(self.engine.account_data_root('fw867'), NAS_DATA_ROOT)

    def test_browse_reports_missing_root(self):
        self.build()
        self.data_dir().rmdir()
        with self.assertRaises(Error) as caught:
            self.engine.browse_account_dirs('fw867')
        self.assertIn('数据目录不存在', str(caught.exception))


class BrowseAccountDirsTests(EngineHarness):
    """弹窗里的目录列表：只列目录、跳过隐藏目录与符号链接、标注已共享。"""

    def test_lists_only_subdirectories_with_shared_flags(self):
        self.build()
        self.data_dir()
        photos = self.data_child('照片')
        videos = self.data_child('视频')
        # 默认配置里 `我的照片` 已经由 App 的共享段覆盖（段 id u3943892_nb_1）
        shared_dir = self.data_child('我的照片')
        self.data_file('readme.txt')                        # 文件不列
        self.data_child('.hidden')                          # 隐藏目录不列

        data = self.engine.browse_account_dirs('fw867')

        self.assertTrue(data['ok'])
        self.assertEqual(data['account'], 'fw867')
        self.assertEqual(data['root'], NAS_DATA_ROOT)
        names = [item['name'] for item in data['dirs']]
        self.assertEqual(names, sorted(['照片', '视频', '我的照片']))
        self.assertNotIn('readme.txt', names)
        self.assertNotIn('.hidden', names)
        by_path = {item['path']: item for item in data['dirs']}
        self.assertTrue(by_path[shared_dir]['shared'])
        self.assertEqual(by_path[shared_dir]['shareName'], 'u3943892_nb_1')
        self.assertEqual(by_path[shared_dir]['display'], '照片-3943892')
        self.assertFalse(by_path[photos]['shared'])
        self.assertEqual(by_path[photos]['shareName'], '')
        self.assertFalse(by_path[videos]['shared'])

    def test_directory_shared_by_the_plugin_is_marked(self):
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_child('下载')
        self.engine.add_share('fw867', target)
        data = self.engine.browse_account_dirs('fw867')
        item = next(entry for entry in data['dirs'] if entry['path'] == target)
        self.assertTrue(item['shared'])
        self.assertEqual(item['shareName'], 'fw867_nb_1')
        self.assertEqual(item['display'], '下载')
        # 插件自建的共享可以删：弹窗里的勾选框要能取消
        self.assertTrue(item['deletable'])

    def test_deletable_flag_distinguishes_owner_and_unshared(self):
        """`deletable`：插件自建 True、官方 App 建的那条 False、未共享 False。"""
        self.build()
        self.manager()
        self.drop_list_dirs()
        own = self.data_child('下载')
        self.engine.add_share('fw867', own)
        official = self.data_child('我的照片')           # 默认配置里由段 id u3943892_nb_1 共享
        free = self.data_child('笔记')

        by_path = {item['path']: item
                   for item in self.engine.browse_account_dirs('fw867')['dirs']}

        self.assertTrue(by_path[own]['shared'])
        self.assertTrue(by_path[own]['deletable'])
        self.assertTrue(by_path[official]['shared'])
        self.assertFalse(by_path[official]['deletable'])       # 官方 App 建的：锁死
        self.assertFalse(by_path[free]['shared'])
        self.assertFalse(by_path[free]['deletable'])
        # 其它字段一个都没变
        for item in by_path.values():
            self.assertEqual(sorted(item),
                             ['deletable', 'display', 'name', 'path', 'shareName', 'shared'])

    def test_directory_listed_without_a_share_section_is_not_shared(self):
        """`list dirs` 里有目录但没有共享段：它不是共享，仍要能勾选。"""
        self.build()
        self.manager()
        self.data_child('下载')

        listed = next(share for share in self.engine.accounts()[0]['shares']
                      if share['path'] == NAS_DATA_ROOT + '/下载')
        self.assertTrue(listed['missing'])          # 引擎如实标成「配置里有目录、没有共享段」
        self.assertEqual(listed['name'], '')

        data = self.engine.browse_account_dirs('fw867')
        item = next(entry for entry in data['dirs'] if entry['name'] == '下载')
        self.assertFalse(item['shared'])
        self.assertEqual(item['shareName'], '')
        self.assertEqual(item['display'], '')

    def test_symlinked_directory_is_skipped(self):
        self.build()
        self.data_dir()
        self.data_child('真实目录')
        made = self.sandbox.symlink(self.data_child('真实目录'),
                                    NAS_DATA_ROOT + '/软链接')
        if not made:
            self.skipTest('当前平台不支持创建符号链接')
        names = [item['name'] for item in self.engine.browse_account_dirs('fw867')['dirs']]
        self.assertIn('真实目录', names)
        self.assertNotIn('软链接', names)

    def test_dirs_are_sorted_by_name(self):
        self.build()
        self.data_dir()
        for name in ('b', 'a', 'c'):
            self.data_child(name)
        # 排序按名字（与列表接口一致）；中文与 ASCII 混排也必须是稳定顺序
        names = [item['name'] for item in self.engine.browse_account_dirs('fw867')['dirs']]
        self.assertEqual(names, sorted(names))

    # ---- 位置分组（存储池 / 外接设备）------------------------------------
    def test_locations_group_pool_and_nas_mnt(self):
        """`/nas/mnt` 下的一层子目录也列出来：存储池 + 外接设备两个位置。"""
        self.build()
        self.data_dir()
        photos = self.data_child('照片')
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            usb = self.sandbox.makedirs('/nas/mnt/usb')
            pa0 = self.sandbox.makedirs('/nas/mnt/pa0')
            self.sandbox.makedirs('/nas/mnt/usb/下载')      # 更深一层不属于「一层子目录」
            self.sandbox.makedirs('/nas/mnt/.hidden')       # 隐藏目录不列
            (self.sandbox.real('/nas/mnt') / 'readme.txt').write_text('x', encoding='utf-8')
            data = self.engine.browse_account_dirs('fw867')

        self.assertEqual([item['root'] for item in data['locations']],
                         [NAS_DATA_ROOT, NAS_MNT_ROOT])
        self.assertEqual([item['label'] for item in data['locations']], ['存储池', 'mnt'])
        self.assertTrue(all(item['available'] for item in data['locations']))
        self.assertEqual(data['locations'][0]['dirs'], data['dirs'])     # 老结构原样保留
        self.assertEqual([item['name'] for item in data['dirs']], sorted(['照片']))
        by_path = {item['path']: item for item in data['locations'][1]['dirs']}
        self.assertEqual(sorted(by_path), sorted([usb, pa0]))            # 文件/隐藏目录不列
        self.assertFalse(by_path[usb]['shared'])                         # 未共享 → 可勾选
        self.assertEqual(by_path[usb]['shareName'], '')
        self.assertFalse(by_path[usb]['deletable'])
        self.assertEqual(by_path[usb]['name'], 'usb')
        self.assertNotIn(photos, by_path)

    def test_locations_mark_a_missing_extra_root_as_unavailable(self):
        """外接设备没插上（`/nas/mnt` 不存在）：位置标成未接入，但不报错。"""
        self.build()
        self.data_dir()
        self.data_child('照片')
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            data = self.engine.browse_account_dirs('fw867')

        pool, extra = data['locations']
        self.assertTrue(pool['available'])
        self.assertEqual([item['name'] for item in pool['dirs']], ['照片'])
        self.assertEqual(extra['root'], NAS_MNT_ROOT)
        self.assertFalse(extra['available'])
        self.assertEqual(extra['dirs'], [])

    def test_locations_follow_custom_extra_roots(self):
        self.build()
        self.data_dir()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt', '/data/额外')):
            self.sandbox.makedirs('/data/额外')
            data = self.engine.browse_account_dirs('fw867')

        self.assertEqual([item['root'] for item in data['locations']],
                         [NAS_DATA_ROOT, NAS_MNT_ROOT, '/data/额外'])
        # 自定义根的名字取末段目录名；没插上的那个标成未接入
        self.assertEqual([item['label'] for item in data['locations']], ['存储池', 'mnt', '额外'])
        self.assertEqual([item['available'] for item in data['locations']], [True, False, True])

    def test_shared_directory_under_nas_mnt_is_marked(self):
        self.build()
        self.manager()
        self.drop_list_dirs()
        self.data_dir()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            usb = self.sandbox.makedirs('/nas/mnt/usb')
            self.engine.add_share('fw867', usb)
            data = self.engine.browse_account_dirs('fw867')

        item = next(entry for entry in data['locations'][1]['dirs']
                    if entry['path'] == usb)
        self.assertTrue(item['shared'])
        self.assertEqual(item['shareName'], 'fw867_nb_1')
        self.assertTrue(item['deletable'])          # 插件自建的：勾选框可以取消


class ShareLocationsTests(EngineHarness):
    """「添加共享」弹窗的位置列表：存储池 + 外接存储（默认 `/nas/mnt`）。"""

    def test_lists_pool_and_external_storage(self):
        self.build()
        self.data_dir()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            self.sandbox.makedirs('/nas/mnt/usb')
            locations = self.engine.share_locations('fw867')

        self.assertEqual([item['index'] for item in locations], [0, 1])
        self.assertEqual(locations[0]['label'], '存储池')
        self.assertEqual(locations[0]['path'], NAS_DATA_ROOT)
        self.assertTrue(locations[0]['exists'])
        # 弹窗里 /nas/mnt 显示成「外接存储」（比末段名 mnt 易懂）
        self.assertEqual(locations[1]['label'], '外接存储')
        self.assertEqual(locations[1]['path'], NAS_MNT_ROOT)
        self.assertTrue(locations[1]['exists'])

    def test_marks_a_missing_external_root_as_not_present(self):
        self.build()
        self.data_dir()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            locations = self.engine.share_locations('fw867')
        self.assertEqual(locations[1]['path'], NAS_MNT_ROOT)
        self.assertFalse(locations[1]['exists'])
        self.assertEqual(self.engine.extra_location_paths(), [NAS_MNT_ROOT])  # 未接入也要列出

    def test_unknown_account_is_rejected(self):
        self.build()
        self.data_dir()
        with self.assertRaises(Error) as caught:
            self.engine.share_locations('nobody')
        self.assertIn('没有这个账号', str(caught.exception))

    def test_browse_label_override_only_for_nas_mnt(self):
        self.assertEqual(engine.browse_location_label('/nas/mnt'), '外接存储')
        self.assertEqual(engine.browse_location_label('/nas/mnt/usb'), '外接设备')
        self.assertEqual(engine.browse_location_label('/nas/mnt/pa0'), 'pa0')
        self.assertEqual(engine.browse_location_label(NAS_DATA_ROOT), '存储池')


class BrowseShareDirsTests(EngineHarness):
    """逐层浏览（`browse_share_dirs`）：一层子目录、跳过隐藏/软链、越界与不存在报错。"""

    def test_lists_one_level_of_directories(self):
        self.build()
        self.data_dir()
        photos = self.data_child('照片')
        self.data_child('视频')
        self.data_file('readme.txt')                     # 文件不列
        self.data_child('.hidden')                       # 隐藏目录不列

        data = self.engine.browse_share_dirs('fw867')

        self.assertTrue(data['ok'])
        self.assertEqual(data['root'], 0)
        self.assertEqual(data['path'], '')
        self.assertEqual(data['absolute'], NAS_DATA_ROOT)
        self.assertTrue(data['exists'])
        self.assertEqual([item['name'] for item in data['items']], sorted(['照片', '视频']))
        by_name = {item['name']: item for item in data['items']}
        self.assertEqual(by_name['照片']['path'], '照片')                  # 相对路径
        self.assertEqual(by_name['照片']['absolute'], photos)              # 绝对路径
        self.assertFalse(by_name['照片']['shared'])
        self.assertEqual([item['label'] for item in data['locations']][0], '存储池')
        self.assertNotIn('readme.txt', by_name)
        self.assertNotIn('.hidden', by_name)

    def test_enters_a_subdirectory_with_a_relative_path(self):
        self.build()
        self.data_dir()
        nested = self.data_child('照片')
        (self.sandbox.real(nested) / '2024').mkdir()
        data = self.engine.browse_share_dirs('fw867', 0, '照片')
        self.assertEqual(data['path'], '照片')
        self.assertEqual(data['absolute'], nested)
        self.assertEqual([item['name'] for item in data['items']], ['2024'])
        self.assertEqual(data['items'][0]['path'], '照片/2024')
        self.assertEqual(data['items'][0]['absolute'], nested + '/2024')

    def test_marks_already_shared_directories(self):
        self.build()
        self.manager()
        self.drop_list_dirs()
        target = self.data_child('照片')
        self.engine.add_share('fw867', target)
        data = self.engine.browse_share_dirs('fw867')
        item = next(entry for entry in data['items'] if entry['name'] == '照片')
        self.assertTrue(item['shared'])

    def test_symlinked_directory_is_skipped(self):
        self.build()
        self.data_dir()
        real = self.data_child('真实目录')
        made = self.sandbox.symlink(real, NAS_DATA_ROOT + '/软链接')
        if not made:
            self.skipTest('当前平台不支持创建符号链接')
        names = [item['name'] for item in self.engine.browse_share_dirs('fw867')['items']]
        self.assertIn('真实目录', names)
        self.assertNotIn('软链接', names)

    def test_rejects_traversal_and_absolute_paths(self):
        self.build()
        self.data_dir()
        self.data_child('照片')
        for bad in ('..', '../..', '照片/../..', '/etc'):
            with self.subTest(path=bad), self.assertRaises(Error):
                self.engine.browse_share_dirs('fw867', 0, bad)

    def test_rejects_missing_and_non_directory_paths(self):
        self.build()
        self.data_dir()
        self.data_child('照片')
        self.data_file('readme.txt')
        with self.assertRaises(Error) as caught:
            self.engine.browse_share_dirs('fw867', 0, '不存在')
        self.assertIn('目录不存在', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.browse_share_dirs('fw867', 0, 'readme.txt')
        self.assertIn('不是一个目录', str(caught.exception))

    def test_rejects_an_invalid_location_index(self):
        self.build()
        self.data_dir()
        for bad in ('9', '-1', 'x'):
            with self.subTest(root=bad), self.assertRaises(Error) as caught:
                self.engine.browse_share_dirs('fw867', bad)
            self.assertIn('位置无效', str(caught.exception))

    def test_browses_the_external_storage_location(self):
        self.build()
        self.data_dir()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            usb = self.sandbox.makedirs('/nas/mnt/usb')
            self.sandbox.makedirs('/nas/mnt/usb/下载')
            (self.sandbox.real('/nas/mnt/usb') / 'readme.txt').write_text(
                'x', encoding='utf-8')
            data = self.engine.browse_share_dirs('fw867', 1, 'usb')

        self.assertEqual(data['root'], 1)
        self.assertEqual(data['absolute'], usb)
        self.assertEqual([item['name'] for item in data['items']], ['下载'])
        self.assertEqual(data['items'][0]['absolute'], '/nas/mnt/usb/下载')
        self.assertEqual(data['locations'][1]['label'], '外接存储')

    def test_browsing_a_missing_location_reports_not_available(self):
        self.build()
        self.data_dir()
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            with self.assertRaises(Error) as caught:
                self.engine.browse_share_dirs('fw867', 1, 'usb')
        self.assertIn('未接入', str(caught.exception))
        # 位置根本身也不存在：同样给中文原因（不抛未处理的异常）
        with patch.object(engine, 'EXTRA_ROOTS', ('/nas/mnt',)):
            with self.assertRaises(Error) as caught:
                self.engine.browse_share_dirs('fw867', 1)
        self.assertIn('目录不存在', str(caught.exception))


class AddSharesBatchTests(EngineHarness):
    """多目录一次提交：只跑一次 init_config + reload，失败的如实报出来。"""

    def prepare(self, *names):
        self.build()
        self.manager()
        self.drop_list_dirs()
        return [self.data_child(name) for name in names]

    def test_multiple_directories_reload_only_once(self):
        first, second = self.prepare('照片', '视频')
        result = self.engine.add_shares('fw867', [first, second])

        self.assertEqual([item['shareName'] for item in result['added']],
                         ['fw867_nb_1', 'fw867_nb_2'])
        self.assertEqual([item['path'] for item in result['added']], [first, second])
        self.assertEqual([item['sharePoint'] for item in result['added']], ['照片', '视频'])
        self.assertTrue(all(item['verified'] for item in result['added']))
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.runner.count('add_dir'), 2)
        self.assertEqual(self.runner.count('init_config'), 1)
        self.assertEqual(
            self.runner.calls.count(['/bin/systemctl', 'reload', 'smb', 'nmb']), 1)
        # 段 id 与显示名都写进配置了
        text = self.sandbox.sambashare.read_text(encoding='utf-8')
        for needle in ("config sambashare 'fw867_nb_1'", "option name '照片'",
                       "config sambashare 'fw867_nb_2'", "option name '视频'"):
            self.assertIn(needle, text)

    def test_skips_already_shared_directory(self):
        first, second = self.prepare('照片', '视频')
        self.engine.add_shares('fw867', [first])
        self.runner.calls.clear()

        result = self.engine.add_shares('fw867', [first, second])

        self.assertEqual([item['path'] for item in result['added']], [second])
        self.assertEqual(result['added'][0]['shareName'], 'fw867_nb_2')
        self.assertEqual(len(result['skipped']), 1)
        skipped = result['skipped'][0]
        self.assertEqual(skipped['path'], first)
        self.assertIn('已经是账号 fw867 的共享', skipped['error'])
        self.assertEqual(skipped['shareName'], 'fw867_nb_1')
        self.assertEqual(self.runner.count('add_dir'), 1)

    def test_skips_duplicate_paths_in_one_request(self):
        first, second = self.prepare('照片', '视频')
        result = self.engine.add_shares('fw867', [first, first, second])
        self.assertEqual([item['path'] for item in result['added']], [first, second])
        self.assertEqual(len(result['skipped']), 1)
        self.assertIn('两次', result['skipped'][0]['error'])

    def test_one_failure_does_not_stop_the_others(self):
        first, second, third = self.prepare('照片', '视频', '音乐')
        handler = self.runner.handler

        def flaky(runner, key, timeout):
            if 'add_dir' in key and '视频' in ' '.join(key):
                return Result(1, '', 'share exists\n')
            return handler(runner, key, timeout)

        self.runner.handler = flaky
        result = self.engine.add_shares('fw867', [first, second, third])

        self.assertEqual([item['path'] for item in result['added']], [first, third])
        self.assertEqual([item['shareName'] for item in result['added']],
                         ['fw867_nb_1', 'fw867_nb_3'])       # 序号连续，失败的跳过
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['errors'][0]['path'], second)
        self.assertIn('共享名已存在', result['errors'][0]['error'])
        self.assertEqual(self.runner.count('add_dir'), 3)   # 失败的也调用过，其余没有被中断
        self.assertEqual(self.runner.count('init_config'), 1)

    def test_reports_verification_failure_per_directory(self):
        """只让第一个目录真的进 smb.conf：第二个要被如实报出来，而不是整体成功。"""
        first, second = self.prepare('照片', '视频')
        original = self.runner.handler

        def handler(runner, key, timeout):
            if 'init_config' in key:
                # 跳过成功管理器的「按 sambashare 重新生成」：只留下第一个共享的段
                self.sandbox.smb_conf.write_text(
                    '[global]\n%s\n' % ''.join('[%s]\n' % name for name in ('照片',)),
                    encoding='utf-8')
                return Result(0, 'ok\n', '')
            return original(runner, key, timeout)

        self.runner.handler = handler
        result = self.engine.add_shares('fw867', [first, second])

        self.assertEqual([item['shareName'] for item in result['added']],
                         ['fw867_nb_1', 'fw867_nb_2'])
        self.assertEqual([item['verified'] for item in result['added']], [True, False])
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['errors'][0]['shareName'], 'fw867_nb_2')
        self.assertEqual(result['errors'][0]['path'], second)
        self.assertIn('没有出现在 /var/etc/smb.conf', result['errors'][0]['error'])
        self.assertEqual(self.runner.count('init_config'), 1)

    def test_empty_paths_are_reported_not_crashed(self):
        self.build()
        self.manager()
        with self.assertRaises(Error) as caught:
            self.engine.add_shares('fw867', [])
        self.assertIn('请选择要共享的目录', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.add_shares('fw867', ['', '   '])
        self.assertIn('请选择要共享的目录', str(caught.exception))
        # 空列表 + add_dir 都没得跑：不产生任何命令调用
        self.assertIsNone(self.runner.argv_for('add_dir'))
        self.assertIsNone(self.runner.argv_for('init_config'))

    def test_unknown_account_is_rejected(self):
        self.build()
        self.manager()
        with self.assertRaises(Error) as caught:
            self.engine.add_shares('nobody', [NAS_DATA_ROOT])
        self.assertIn('没有这个账号', str(caught.exception))

    def test_single_path_keeps_the_old_behaviour(self):
        """单目录时沿用 add_share：非法路径 / 已共享 / add_dir 失败都要抛 Error。"""
        first = self.prepare('照片')[0]
        self.engine.add_share('fw867', first)
        with self.assertRaises(Error) as caught:
            self.engine.add_share('fw867', first)
        self.assertIn('已经是账号', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.add_share('fw867', '/etc')
        self.assertIn('不在允许的根目录内', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.add_share('fw867', '')
        self.assertIn('路径', str(caught.exception))
        with self.assertRaises(Error) as caught:
            self.engine.add_shares('fw867', [first], mode='single')
        self.assertIn('已经是账号', str(caught.exception))

    def test_single_path_result_keeps_legacy_fields(self):
        first = self.prepare('照片')[0]
        result = self.engine.add_share('fw867', first, share_point='照片-3943892')
        for key in ('shareName', 'sharePoint', 'verified', 'inConfig', 'smbActive',
                    'reloadReturncode', 'users', 'forceUser', 'added', 'skipped', 'errors'):
            self.assertIn(key, result)
        self.assertEqual(result['sharePoint'], '照片-3943892')
        self.assertEqual(len(result['added']), 1)


if __name__ == '__main__':
    unittest.main()
