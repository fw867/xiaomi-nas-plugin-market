from __future__ import annotations

import os
import posixpath
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
PREFIX_TABLE = (
    (NAS_DATA_ROOT, 'home/u3943892/pool0/data'),
    (NAS_HOME_ROOT, 'home'),
    (NAS_POOL_ROOT, 'nas'),
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
        for name in ('home', 'nas'):
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
        self.patches = [
            patch.object(engine, 'APP_ROOT', self.root / 'etc'),
            patch.object(engine, 'VAR_ETC', self.root / 'var' / 'etc'),
            patch.object(engine, 'DATA_DIR', self.data),
            # 路径存在性/真实路径全部走可注入钩子：受管路径是 POSIX 语义，
            # 在 Windows 上只能靠这三条钩子映射到沙箱里的真实目录。
            patch.object(engine, 'path_exists', self.path_exists),
            patch.object(engine, 'path_isdir', self.path_isdir),
            patch.object(engine, 'path_realpath', self.path_realpath),
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
    - `init_config` 按当前 sambashare 重新生成 smb.conf 的 section 列表；
    - `systemctl show -p UnitFileState` 回答单元状态；
    - 3702 的占用状态：兜底 kill 生效或 drop-in 写好之后才算释放。
    """

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
                '[global]\n' + ''.join('[%s]\n' % item for item in displays), encoding='utf-8')
            return Result(0, 'ok\n', '')
        if 'init_config' in key:
            names = []
            for line in sandbox.sambashare.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('option name '):
                    names.append(line.split("'")[1])
            sandbox.smb_conf.write_text(
                '[global]\n' + ''.join('[%s]\n' % name for name in names), encoding='utf-8')
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
        with patch.dict(os.environ, {'ALLOWED_ROOTS': '/data/a:/data/b'}):
            self.assertEqual(engine.resolve_allowed_roots(None), ['/data/a', '/data/b'])

    def test_fallback_when_config_is_empty(self):
        with patch.dict(os.environ, {'ALLOWED_ROOTS': ''}):
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


if __name__ == '__main__':
    unittest.main()
