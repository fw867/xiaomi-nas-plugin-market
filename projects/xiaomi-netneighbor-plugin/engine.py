#!/usr/bin/env python3
"""缃戠粶閭诲眳鎻掍欢鐨勬牳蹇冮€昏緫锛歐SD 鍥炲簲鍣ㄦ帴绠?+ Samba 鍏变韩鐩綍绠＄悊銆?
杩欎釜妯″潡鍙仛涓変欢浜嬶紝鍏ㄩ儴涓庣郴缁熶氦浜掔殑鍦版柟閮界暀浜嗘敞鍏ョ偣锛坄runner`銆乣samba_mgr`銆?`systemctl`銆乣responder_factory`锛夛紝鎵€浠ュ崟鍏冩祴璇曞彲浠ュ湪瀹屽叏涓嶇绯荤粺鐨勬儏鍐典笅璺戯細

1. **鍙戠幇鏈嶅姟锛堝彲閫夊紑鍏筹級**锛氶〉闈笂鐨勩€岀綉缁滃彂鐜般€嶅紑鍏虫墦寮€鏃讹紝鍚姩鏃?   `systemctl stop wsdd` + `killall wsdd` 鍏滃簳锛屽啀鐢?systemd drop-in
   锛坄/etc/systemd/system/wsdd.service.d/netneighbor.conf`锛夋妸瀹樻柟鍗曞厓鍙樻垚
   **绌烘搷浣?*锛岀劧鍚庡湪**鏈繘绋嬪唴**鐢?`wsd.WsdResponder` 璧蜂竴涓嚜宸辩殑鍥炲簲鍣?   锛堝畼鏂?wsdd 缂?`wsd:AppSequence`銆佸張涓嶅搷搴?WS-Transfer `Get`锛岃 wsd.py 鐨勮鏄庯級锛?   鍏抽棴鏃讹紙鎴栧仠姝?鍗歌浇锛夊彂 Bye銆佸垹鎺?drop-in銆乣daemon-reload` 骞舵妸瀹樻柟鏈嶅姟璧峰洖鏉ャ€?   寮€鍏崇姸鎬佽惤鐩樺湪 `<DATA_DIR>/settings.json`锛屾湇鍔￠噸鍚悗鎸夎惤鐩樼姸鎬佺敓鏁堛€?2. **璐﹀彿 鈫?鍏变韩鐩綍**锛氳В鏋?`/etc/config/sambauser`銆乣/etc/config/sambashare` 涓?   `/etc/samba/users.map`锛屽垪鍑烘瘡涓处鍙峰綋鍓嶆湁鍝簺鍏变韩锛涙柊澧?鍒犻櫎璧?`smb_mgr.sh`銆?   鎵归噺鏂板锛坄add_shares`锛夊湪**鎵€鏈?* `add_dir` 涔嬪悗鍙窇涓€娆?`init_config` + reload锛?   鍐嶉€愪釜鏍稿锛坄share_in_config`锛夈€?3. **鍛藉悕绌洪棿**锛氭彃浠惰嚜宸卞缓鐨勫叡浜彨 `<璐﹀彿>_nb_<搴忓彿>`锛屾案杩滀笉鍜?App 鐢熸垚鐨?   `<璐﹀彿>_<id>`锛堜互鍙?`public`锛夋挒鍚嶏紱鍒犻櫎鏃朵篃鍙厑璁稿垹鑷繁杩欎竴鏃忋€?
鐪熸満锛圢AS 192.168.1.8锛夊疄娴嬬‘璁ょ殑纭害鏉燂紙鍒寜銆岀湅璧锋潵鍚堢悊銆嶆敼鍥炲幓锛夛細

- `smb_mgr.sh shares add_dir` 鐨?`<user_list>` 瑕佷紶**璐﹀彿鍚?*锛堝 `fw867`锛夛紝
  `smb_mgr.sh` 鑷繁浼氬姞 `samba` 鍓嶇紑锛涗紶 `sambafw867` 浼氬湪 `pdbedit` 閭ｆ澶辫触锛坋xit 1锛夈€?- **鍏变韩鐨勮韩浠芥槸 `/etc/config/sambashare` 鐨勬 id**锛坄smb_mgr.sh` 鐨?`add_dir`/`del_dir`
  鐢ㄧ殑灏辨槸瀹冿級锛宍option name` 鏄祫婧愮鐞嗗櫒閲屾樉绀虹殑**鍏变韩鍚?*锛屼篃灏辨槸 `/var/etc/smb.conf`
  閲岀殑 section 鍚嶃€傜湡鏈烘牱渚嬶細娈?id `u3943892_nb_1` 鈫?`option name '鐓х墖-3943892'` 鈫?  `[鐓х墖-3943892]`銆俙/etc/config/sambauser` 鐨勬 id 鏄?`<NAS鐢ㄦ埛>_<搴忓彿>`锛岃处鍙峰悕鍦?  `option name`锛宍list dirs` 鏄?*鐩綍璺緞**锛屼笉鏄叡浜 id銆?- 鎺ョ瀹樻柟 wsdd 璧?systemd drop-in锛坄ExecStart`/`ExecStop`/`ExecReload` 缃垚
  `/bin/true`锛夛細瀹樻柟 `smb_mgr.sh` 鏀跺熬閭ｅ彞 `systemctl restart wsdd` 渚濇棫杩斿洖 0銆?  鏃╁勾鐢?`systemctl mask` 浼氳閭ｄ竴姝ュ繀鐒惰繑鍥?1锛岀敤鎴峰湪 App 閲岀湅鍒般€屼繚瀛樺叡浜け璐ャ€?  锛堝嵆浣?smb.conf 宸茬粡鍐欏ソ锛夆€斺€旀墍浠ヤ笉瑕侀€€鍥?mask銆?- 娓呯悊娈嬬暀鐨?wsdd 杩涚▼瑕佺敤**鐪熸満涓婂瓨鍦ㄧ殑**鍛戒护锛氳繖鍙?NAS 娌℃湁 `pkill`锛堟壘涓嶅埌鍛戒护锛?  exit 127锛夛紝鍙湁 `killall`锛堝畼鏂?`/usr/bin/wsd` 鍖呰鑴氭湰鐢ㄧ殑涔熸槸 `killall wsdd`锛夛紝
  瑙?`resolve_kill()`锛涗袱涓兘娌℃湁灏卞瀹炶鏃ュ織锛屼笉鍋囪娓呯悊杩囥€?- **涓嶈渚濊禆 `smb_mgr.sh reload` 鐨勯€€鍑虹爜**锛氬畠鏈€鍚庝竴姝ユ槸 `systemctl restart wsdd`锛?  杩欎竴姝ュ湪鏈変簺鏈哄櫒/鏈変簺鐗堟湰涓婁細杩斿洖闈?0锛堣櫧鐒?smb.conf 鍏跺疄宸茬粡鐢熸垚濂斤級銆?  鎵€浠ヨ繖閲岃嚜宸辫蛋 `init_config` + `systemctl reload smb nmb` 涓ゆ锛屽啀鍥炲ご鏍稿閰嶇疆銆?"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import wsd

VERSION = '0.1.1'
PLUGIN_KEY = 'netneighbor'

# ---------------------------------------------------------------------------
# 璺緞涓庡閮ㄥ懡浠わ紙娴嬭瘯閲屽叏閮ㄦ墦妗╋紱Env 瑕嗙洊鏄负浜嗗湪鍒鎺掓煡闂锛?# ---------------------------------------------------------------------------
APP_ROOT = Path(os.environ.get('APP_ROOT', '/etc'))
VAR_ETC = Path(os.environ.get('VAR_ETC', '/var/etc'))
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data/plugin/netneighbor'))
STATE_FILE = DATA_DIR / 'settings.json'
WEB_DIR = Path(os.environ.get('WEB_DIR', str(Path(__file__).resolve().parent / 'web')))

SAMBASHARE_CONFIG = APP_ROOT / 'config' / 'sambashare'
SAMBAUSER_CONFIG = APP_ROOT / 'config' / 'sambauser'
SAMBA_CONFIG = APP_ROOT / 'config' / 'samba'
SMB_USER_MAP = APP_ROOT / 'samba' / 'users.map'
SMB_CONF = VAR_ETC / 'smb.conf'

SMB_MGR = os.environ.get('SMB_MGR') or shutil.which('smb_mgr.sh') or '/usr/bin/smb_mgr.sh'
SYSTEMCTL = os.environ.get('SYSTEMCTL') or shutil.which('systemctl') or '/bin/systemctl'
WSDD_UNIT = os.environ.get('WSDD_UNIT', 'wsdd')
# 娓呯悊娈嬬暀 wsdd 杩涚▼鐨勫厹搴曞懡浠わ細鐪熸満锛?92.168.1.8锛?*娌℃湁 pkill**锛堟壘涓嶅埌鍛戒护锛宔xit 127锛夛紝
# 鍙湁 killall锛屽畼鏂?`/usr/bin/wsd` 鍖呰鑴氭湰鐢ㄧ殑涔熸槸 `killall wsdd`銆傛墍浠ヤ紭鍏?killall銆?KILL_CANDIDATES = tuple(os.environ.get('KILL_CANDIDATES', 'killall pkill').split())
KILL = os.environ.get('KILL', '').strip()       # 鏄惧紡瑕嗙洊锛堟帓鏌?娴嬭瘯鐢級锛涚┖鍒欐寜涓婇潰椤哄簭鎺㈡祴
# 瀹樻柟鍙戠幇鏈嶅姟鐨勩€岀┖鎿嶄綔銆峝rop-in锛氬彧鍔ㄦ垜浠嚜宸辩殑杩欎竴涓枃浠讹紝缁濅笉鏀?/usr/lib 涓嬬殑鍘?unit
WSDD_DROPIN_DIR = Path(os.environ.get(
    'WSDD_DROPIN_DIR', '/etc/systemd/system/%s.service.d' % WSDD_UNIT))
WSDD_DROPIN = WSDD_DROPIN_DIR / 'netneighbor.conf'
WSDD_DROPIN_TEXT = """# 鐢辩綉缁滈偦灞呮彃浠跺啓鍏ワ細鎶婂畼鏂?wsdd 鍙樻垚绌烘搷浣滐紝閬垮厤涓や釜鍙戠幇鏈嶅姟鎶?3702銆?# 鍒犻櫎鏈枃浠跺苟 systemctl daemon-reload 鍗冲彲瀹屽叏鎭㈠瀹樻柟琛屼负銆?[Service]
ExecStart=
ExecStart=/bin/true
ExecStop=
ExecStop=/bin/true
ExecReload=
ExecReload=/bin/true
RemainAfterExit=yes
"""
SMB_UNITS = tuple(os.environ.get('SMB_UNITS', 'smb nmb').split())
# 瀹樻柟 wsdd 閫€鍑哄悗 3702 涓€鑸珛鍒婚噴鏀撅紱缁欏畠涓€鐐规椂闂达紙鏈€澶?~3 绉掞級
WSD_PORT_WAIT = float(os.environ.get('WSD_PORT_WAIT', '3'))
# 鍥炲簲鍣ㄥ惎鍔ㄥけ璐ユ椂閲嶈瘯鍑犳锛氱湡鏈轰笂銆屽仠瀹樻柟 wsdd銆嶅拰銆岃嚜宸辩粦 3702銆嶄箣闂存湁绔炴€?# 锛堝仠鍦ㄥ惎鍔ㄤ腑鐨勮繘绋嬩笂銆佺鍙ｈ繕娌℃斁寮€锛夛紝涓€娆″け璐ュ氨姘镐箙鏀惧純浼氳椤甸潰涓€鐩淬€屾湭杩愯銆嶃€?RESPONDER_START_RETRIES = int(os.environ.get('RESPONDER_START_RETRIES', '5'))
RESPONDER_START_GAP = float(os.environ.get('RESPONDER_START_GAP', '1'))

# 鍏冩暟鎹?HTTP 鏈嶅姟绔彛锛歐indows 鎸?XAddrs 鏉ュ彇璁惧鎻忚堪锛?357 鏄?WSD 鐨勬儻渚嬬鍙?METADATA_PORT = int(os.environ.get('WSD_METADATA_PORT', '5357'))
HELLO_INTERVAL = int(os.environ.get('WSD_HELLO_INTERVAL', '900'))

DEFAULT_HOSTNAME = os.environ.get('DEFAULT_HOSTNAME', 'SmartStorage')
DEFAULT_WORKGROUP = os.environ.get('DEFAULT_WORKGROUP', 'WORKGROUP')

# 绯荤粺涓绘満鍚嶏紙Windows 11 闈?WSD/mDNS/DNS 鍙戠幇璁惧鏃剁敤鐨勫氨鏄畠锛夊湪 uci 閲岀殑浣嶇疆銆?# Samba 鐨?`netbios name`锛? /etc/config/samba 鐨?`option name`锛夊彧褰卞搷鑰佸紡 NetBIOS 娴忚锛?# 涓よ€呬笉涓€鑷存椂鐢ㄦ埛鐢ㄥ摢涓悕瀛楅兘鍙兘杩炰笉涓娾€斺€旀墍浠ラ〉闈㈠彧璇诲睍绀鸿繖涓や釜鍚嶅瓧锛屼笉鍐嶆彁渚涙敼鍚嶃€?SYSTEM_HOSTNAME_OPTION = os.environ.get('SYSTEM_HOSTNAME_OPTION', 'system.@system[0].hostname')
SMB_NETBIOS_OPTION = 'netbios name'
# 銆屾仮澶嶄负绯荤粺涓绘満鍚嶃€嶈閲嶅惎鐨勬湇鍔★細`netbios name` 鍙?reload 涓嶇敓鏁堬紝蹇呴』 restart
SMB_RESTART_UNITS = tuple(os.environ.get('SMB_RESTART_UNITS', 'smb nmb wsdd').split())

# 鍏佽浣滀负鍏变韩鏍圭殑鐩綍鐧藉悕鍗?== 鐢ㄦ埛鑷繁宸叉湁鍏变韩鎵€鍦ㄧ殑鏍?+ 鍏滃簳鎸傝浇鐐广€?#   /home/<uXXXX>/pool0/data  鈥斺€?鐢ㄦ埛鏁版嵁鐩綍锛堝疄娴?/home/u3943892/pool0/data锛?#   /nas/pool0                鈥斺€?瀛樺偍姹犵殑瀵瑰鎸傝浇鐐?# 鍙互鐢?ALLOWED_ROOTS=/a:/b 鏄惧紡瑕嗙洊锛堝啋鍙峰垎闅旓級锛屾垨鐢?CONFIG_DERIVED_ROOTS=0 鍏虫帀鎺ㄥ銆?FALLBACK_ROOTS = tuple(
    item for item in os.environ.get(
        'ALLOWED_ROOTS_FALLBACK', '/home/*/pool0/data:/nas/pool0').split(':') if item)
CONFIG_DERIVED_ROOTS = os.environ.get('CONFIG_DERIVED_ROOTS', '1') != '0'
SYMLINK_ROOT = '/home'

def parse_extra_roots(value) -> tuple:
    """`EXTRA_ROOTS` 鐨勮В鏋愶細鍐掑彿鍒嗛殧銆佷涪鎺夌┖鐧介」銆?
    **绌轰覆 鈫?绌哄厓缁?*锛堜竴涓澶栨牴閮戒笉杩藉姞锛夛紝杩欐槸銆岃繍缁撮攣姝诲彲鍏变韩鑼冨洿銆嶇殑寮€鍏筹細
    `ALLOWED_ROOTS=/a:/b EXTRA_ROOTS=` 鏃?`/a:/b` 灏辨槸涓ユ牸鐨勬渶缁堢櫧鍚嶅崟銆?    鏀惧湪甯搁噺鍓嶉潰鏄洜涓烘ā鍧楀鍏ユ椂灏辫鐢ㄥ畠瑙ｆ瀽鐜鍙橀噺銆?    """
    return tuple(item.strip() for item in str(value or '').split(':') if item.strip())


# **杩藉姞**鐨勫厑璁稿叡浜牴锛堝鎺ヨ澶?/ U 鐩?/ 鍏跺畠鎸傝浇鐐癸紝榛樿 `/nas/mnt`锛夛細
# 鐪熸満涓?NAS 鏈変釜鏈嶅姟鎶?`/nas/mnt/usb` bind 鍒板巶鍟嗚嚜宸辩殑鎸傝浇鐐?`/mnt/usb-<鍝堝笇>` 涓婏紝
# 鎷旂洏鏃?`/nas/mnt/usb` 杩欎釜璺緞灏变笉瀛樺湪浜嗐€俙/nas/mnt` 涓嬮潰鏄?usb銆乸a0銆乸a1 杩欎簺
# 鎸傝浇鐐癸紝鎵€浠ユ妸鏁翠釜 `/nas/mnt` 浣滀负鏍癸紝瀹冧笅闈㈢殑瀛愮洰褰曞氨閮借兘鏍￠獙銆佸叡浜€?# 涓?`ALLOWED_ROOTS` 鐨勩€屾樉寮忚鐩栥€嶈涔変笉鍚岋紝`EXTRA_ROOTS` 鏄線鏈€缁堝垪琛ㄩ噷**杩藉姞**锛?#   鏈€缁堟牴 = 锛圓LLOWED_ROOTS 鏈夊垯鍗曠嫭鐢ㄥ畠锛屽惁鍒?FALLBACK_ROOTS + 鎸夐厤缃淳鐢燂級+ EXTRA_ROOTS
# 涓夌鍙栧€硷紙瑙?parse_extra_roots()锛夛細
#   * 鏈缃?      鈫?榛樿 `/nas/mnt`锛屽紑绠卞嵆鍙叡浜鎺ヨ澶囬噷鐨勭洰褰曪紱
#   * 鏄惧紡璁炬垚绌轰覆 鈫?**涓€涓兘涓嶈拷鍔?*锛氳繖鏃?`ALLOWED_ROOTS` 灏辨槸涓ユ牸鐨勬渶缁堢櫧鍚嶅崟
#                    锛坄ALLOWED_ROOTS=/a:/b EXTRA_ROOTS=` 鑳介攣姝诲彲鍏变韩鑼冨洿锛夛紱
#   * 璁句簡鍊?      鈫?鍐掑彿鍒嗛殧杩藉姞锛屾敮鎸佸崟灞傞€氶厤锛堝 `/nas/mnt:/mnt/usb-*`锛夈€?# 鑰屼笖鍙垪**褰撳墠鐪熷疄瀛樺湪**鐨勬牴锛堟嫈鐩樺悗涓嶇暀涓€涓寚鍚戠┖璺緞鐨勬牴锛岃 extra_roots()锛夈€?# 榛樿鍚屾椂鏀?`/mnt/usb-*`锛歚/nas/mnt/usb` 鏄?*杞摼**鏃讹紝`realpath()` 浼氳В鏋愭垚鍘傚晢鐨?# 鐪熷疄鎸傝浇鐐?`/mnt/usb-<鍝堝笇>`锛涘彧鎶?`/nas/mnt` 鏀捐繘鐧藉悕鍗曠殑璇濓紝`validate_share_path()`
# 鐨勩€岀湡瀹炶矾寰勪篃瑕佸湪鐧藉悕鍗曞唴銆嶆鏌ヤ細澶辫触锛堝疄娴?400锛氱洰褰曡В鏋愬埌鐨勭湡瀹炶矾寰勪笉鍦ㄥ厑璁歌寖鍥村唴锛夈€?# 杞摼/鐪熷疄璺緞涓ょ鍐欐硶閮借鑳藉叡浜紝鎵€浠ヤ袱涓牴閮芥敹銆?EXTRA_ROOTS = parse_extra_roots(
    os.environ.get('EXTRA_ROOTS', '/nas/mnt:/mnt/usb-*'))

# 寮圭獥閲屻€屼綅缃€嶅垎缁勭殑鏍囩瑙勫垯锛堢函瀛楃涓插尮閰嶏紝瑙?location_label()锛夛細
#   /nas/pool0 鎴?/home/<NAS鐢ㄦ埛>/pool0 涔嬩笅 鈫?瀛樺偍姹狅紱
#   /nas/mnt/usb* 鎴?/mnt/usb-* 涔嬩笅 鈫?澶栨帴璁惧锛?#   鍏朵綑锛堝 /nas/mnt銆?nas/mnt/pa0锛夊彇鏈鐩綍鍚嶃€?POOL_ROOT_PATTERNS = ('/nas/pool0', '/home/*/pool0')
EXTRA_ROOT_PATTERNS = ('/nas/mnt/usb', '/nas/mnt/usb*', '/mnt/usb-*')
# 鐩綍娴忚鍣ㄩ噷浣嶇疆鎸夐挳鐨勬爣棰樿鐩栵細`/nas/mnt` 鈫?銆屽鎺ュ瓨鍌ㄣ€嶏紙鏈鍚?mnt 瀵圭敤鎴锋病鎰忎箟锛?BROWSE_LABEL_OVERRIDES = {'/nas/mnt': '澶栨帴瀛樺偍'}

# 鎻掍欢鑷缓鍏变韩鐨勫懡鍚嶇┖闂达細<璐﹀彿>_nb_<搴忓彿>
SHARE_PREFIX = '_nb_'
# 娈?id锛堝叡浜爣璇嗭級浠嶇劧鏄函 ASCII锛涙樉绀哄悕锛坥ption name锛夊彲浠ユ槸涓枃锛堢湡鏈猴細'鐓х墖-3943892'锛夛紝
# 鍙涓嶅惈绌虹櫧銆佽矾寰勫垎闅旂涓?smb.conf 娈靛悕閲岀殑鐗规畩瀛楃鍗冲彲銆?SHARE_NAME_PATTERN = re.compile(r'^[^\s/\\\[\]:*?"<>|\x00-\x1f\x7f]{1,64}$')
ACCOUNT_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$')
NAS_USER_PATTERN = re.compile(r'^u[0-9]{3,}$')
# 涓绘満鍚嶏紙Windows銆岀綉缁溿€嶉噷鏄剧ず鐨勯偅涓€涓級锛歂etBIOS 鍙嬪ソ锛?-15 浣嶏紝棣栧瓧绗﹀繀椤绘槸瀛楁瘝鎴栨暟瀛?HOSTNAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,14}$')

# 杩欎簺鏄?App / 绯荤粺鍦ㄧ敤鐨勫叡浜紝鎻掍欢涓€寰嬩笉璁稿垹
PROTECTED_SHARES = frozenset({'public', 'homes', 'printers', 'print$'})

COMMAND_TIMEOUT = int(os.environ.get('COMMAND_TIMEOUT', '60'))
UCI_TIMEOUT = 10

# smb_mgr.sh 甯歌浣嶇疆锛岀敤鏉ュ湪榛樿璺緞涓嶅瓨鍦ㄦ椂鍏滃簳
SMB_MGR_CANDIDATES = (
    '/usr/bin/smb_mgr.sh', '/usr/sbin/smb_mgr.sh', '/sbin/smb_mgr.sh',
    '/usr/local/bin/smb_mgr.sh', '/usr/local/sbin/smb_mgr.sh',
)


class Error(Exception):
    """鍙互鐩存帴灞曠ず缁欑敤鎴风殑涓枃閿欒銆?""


class Result:
    """澶栭儴鍛戒护鐨勭粨鏋溿€傚瓧娈典笌 `subprocess.CompletedProcess` 淇濇寔涓€鑷淬€?""

    def __init__(self, returncode: int = 0, stdout: str = '', stderr: str = ''):
        self.returncode = int(returncode)
        self.stdout = stdout or ''
        self.stderr = stderr or ''

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __repr__(self) -> str:                                  # pragma: no cover
        return 'Result(returncode=%r, stdout=%r, stderr=%r)' % (
            self.returncode, self.stdout, self.stderr)


def installed_version() -> str:
    """鎻掍欢鍖呯殑鐪熷疄鐗堟湰锛堢洰褰曞悕閲屽甫鐫€鐗堟湰锛涙簮鐮佹爲閲岃窇灏卞洖閫€鍒?VERSION锛夈€?""
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


# ---------------------------------------------------------------------------
# 鍩虹宸ュ叿
# ---------------------------------------------------------------------------

def run(command: list, timeout: int = COMMAND_TIMEOUT, check: bool = False) -> Result:
    """榛樿鐨勫懡浠ゆ墽琛屽櫒锛氬叏閮ㄦ崟鑾?stdout/stderr锛岃秴鏃舵姏 Error 鑰屼笉鏄寕浣忚姹傘€?""
    try:
        completed = subprocess.run(
            [str(item) for item in command], capture_output=True, text=True,
            check=False, timeout=timeout)
    except FileNotFoundError as error:
        raise Error('鎵句笉鍒板懡浠?%s' % command[0]) from error
    except subprocess.TimeoutExpired as error:
        raise Error('鍛戒护鎵ц瓒呮椂锛?s 绉掞級锛?s'
                    % (timeout, ' '.join(str(c) for c in command))) from error
    except OSError as error:
        raise Error('鎵ц鍛戒护澶辫触锛?s' % error) from error
    if check and completed.returncode != 0:
        raise Error('鍛戒护鎵ц澶辫触锛?s' % describe(command, completed))
    return Result(completed.returncode, completed.stdout, completed.stderr)


def describe(command: list, result: Result) -> str:
    """鎶婁竴娆″閮ㄥ懡浠ょ殑鍘熸枃鎷兼垚涓€娈靛彲灞曠ず銆佸彲鎺掓煡鐨勬枃瀛椼€?""
    lines = ['$ ' + ' '.join(str(item) for item in command)]
    if result.stdout.strip():
        lines.append('stdout: ' + result.stdout.strip())
    if result.stderr.strip():
        lines.append('stderr: ' + result.stderr.strip())
    lines.append('exit code: %d' % result.returncode)
    return '\n'.join(lines)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8', newline='\n')
    temporary.replace(path)


def read_json(path: Path, default):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n')


def normalize_discovery_enabled(value, default: bool = True) -> bool:
    """钀界洏鐨勫紑鍏冲€艰В鏋愶細鍙湁鏄庣‘鐨勫亣鍊兼墠绠椼€屽叧闂€嶏紝鍧忓€煎洖閫€鍒伴粯璁ゃ€?""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'yes', 'on'):
            return True
        if text in ('0', 'false', 'no', 'off', ''):
            return False
    return bool(default)


def load_settings(path=None) -> dict:
    """鎻掍欢鑷繁鐨勮缃紙鍏冩暟鎹鍙ｃ€丠ello 鍛ㄦ湡銆佸彂鐜板紑鍏筹級銆?
    鍧忔枃浠朵笉璇ユ嫋鍨惎鍔細姣忛」閮界嫭绔嬫牎楠岋紝璇讳笉鎳傜殑灏卞洖閫€鍒伴粯璁ゅ€笺€?    `path` 榛樿鏄ā鍧楃骇鐨?`STATE_FILE`锛汦ngine 浼氫紶鑷繁 `data_dir` 涓嬬殑閭ｄ唤锛?    杩欐牱鍗曞厓娴嬭瘯鐨勬矙绠变笌鐪熸満鏁版嵁鐩綍涓嶄細浜掔浉褰卞搷銆?
    **娌℃湁 hostname**锛氫富鏈哄悕鏀规垚鍙灞曠ず锛堟湇鍔＄鑷姩鑾峰彇锛夛紝鏃х増鏈惤鐩樼殑
    `hostname` 閿竴寰嬪拷鐣ワ紙涔熶笉浼氬啀鍐欏洖鍘伙級銆?    """
    saved = read_json(Path(path) if path else STATE_FILE, {})
    settings = {
        'metadataPort': METADATA_PORT,
        'helloInterval': HELLO_INTERVAL,
        'discoveryEnabled': True,       # 榛樿鎺ョ锛堝崌绾у墠鐨勮涓猴級锛屽叧闂悗閲嶅惎涔熶笉浼氳嚜鍔ㄦ帴绠?    }
    if isinstance(saved, dict):
        for key in ('metadataPort', 'helloInterval'):
            value = saved.get(key)
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                settings[key] = int(value)
        if 'discoveryEnabled' in saved:
            settings['discoveryEnabled'] = normalize_discovery_enabled(
                saved.get('discoveryEnabled'))
    settings['metadataPort'] = max(1, min(65535, int(settings['metadataPort'])))
    settings['helloInterval'] = max(0, int(settings['helloInterval']))
    return settings


def save_settings(settings: dict, path=None) -> None:
    write_json(Path(path) if path else STATE_FILE, settings)


# ---------------------------------------------------------------------------
# uci 閰嶇疆瑙ｆ瀽
# ---------------------------------------------------------------------------

def _unquote(text: str) -> str:
    """鍘绘帀 uci 鍗曞紩鍙?鍙屽紩鍙凤紝骞惰В寮€杞箟銆?""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace('\\\\', '\\')
        return inner
    return text


def parse_uci(text: str) -> list:
    """瑙ｆ瀽 uci 瀵煎嚭鏂囨湰锛岃繑鍥?[{'type','name','options','lists'}, ...]銆?
    鍙澶熺敤灏辫锛歚config <type> ['<name>']`銆乣option k 'v'`銆乣list k 'v'`銆?    鏈甫鍚嶅瓧鐨?section 鐢?`cfg%04d` 閫犱竴涓ǔ瀹氬悕瀛楋紙鐪熷疄鏂囦欢閲岄兘鏈夊悕瀛楋級銆?    """
    sections: list = []
    current = None
    anonymous = 0
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        keyword, _, rest = line.partition(' ')
        keyword = keyword.strip()
        rest = rest.strip()
        if keyword == 'config':
            parts = rest.split(None, 1)
            if not parts:
                continue
            kind = parts[0]
            if len(parts) > 1:
                name = _unquote(parts[1])
            else:
                anonymous += 1
                name = 'cfg%04d' % anonymous
            current = {'type': kind, 'name': name, 'options': {}, 'lists': {}}
            sections.append(current)
            continue
        if current is None or keyword not in ('option', 'list'):
            continue
        key, _, value = rest.partition(' ')
        key = key.strip()
        value = _unquote(value)
        if not key:
            continue
        if keyword == 'option':
            current['options'][key] = value
        else:
            current['lists'].setdefault(key, []).append(value)
    return sections


def parse_smb_user_map(text: str) -> dict:
    """`/etc/samba/users.map`锛歚samba<璐﹀彿> = <璐﹀彿>` 鈫?{'samba璐﹀彿': '璐﹀彿'}銆?""
    mapping: dict = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def parse_dirs(value) -> list:
    """`list dirs 'a' 'b'` 涓?`option dirs 'a b'` 涓ょ鍐欐硶閮芥帴鍙椼€?""
    if isinstance(value, (list, tuple)):
        items = []
        for entry in value:
            items.extend(str(entry).split())
        return items
    return str(value or '').split()


def smb_conf_sections(text: str) -> list:
    """`/var/etc/smb.conf` 閲岀殑 section 鍚嶏紙`[鐓х墖-3943892]` 鈫?鐓х墖-3943892锛夈€?""
    names = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if line.startswith('[') and line.endswith(']'):
            names.append(line[1:-1].strip())
    return names


# ---------------------------------------------------------------------------
# smb_mgr.sh 鍛戒护鎷艰锛堢函鍑芥暟锛屼究浜庢柇瑷€ argv锛?# ---------------------------------------------------------------------------

def add_dir_command(share_name: str, path: str, user_list, share_point: str = '',
                    force_user: str = '', smb_mgr: str = '') -> list:
    """`smb_mgr.sh shares add_dir <share_name> <path> "<user_list>" <share_point> [force_user]`

    娉ㄦ剰 `<user_list>` 鏄?*璐﹀彿鍚?*锛坄fw867`锛夛紝涓嶆槸 `sambafw867`锛歴mb_mgr.sh 鑷繁鍔犲墠缂€銆?    """
    users = user_list if isinstance(user_list, str) else ' '.join(str(i) for i in user_list)
    command = [smb_mgr or SMB_MGR, 'shares', 'add_dir', share_name, path, users,
               share_point or share_name]
    if force_user:
        command.append(force_user)
    return command


def del_dir_command(share_name: str, smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'shares', 'del_dir', share_name]


def add_user_command(share_name: str, user_name: str, smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'shares', 'add_user', share_name, user_name]


def del_user_command(share_name: str, user_name: str, smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'shares', 'del_user', share_name, user_name]


def init_config_command(smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'init_config']


def reload_units_command() -> list:
    """`reload` 鐨勬浛浠ｆ柟妗堬細涓や釜鏈嶅姟涓€璧?reload銆?""
    return ['systemctl', 'reload', *SMB_UNITS]


# smb_mgr.sh 澶辫触鏃跺父瑙佺殑鑻辨枃鐭 鈫?涓枃鍘熷洜
MESSAGE_HINTS = (
    ('exist', '鍏变韩鍚嶅凡瀛樺湪'),
    ('not found', '鐩爣涓嶅瓨鍦?),
    ('no such', '鐩爣涓嶅瓨鍦?),
    ('permission', '鏉冮檺涓嶈冻'),
    ('denied', '鏉冮檺涓嶈冻'),
    ('not a directory', '涓嶆槸涓€涓洰褰?),
    ('invalid', '鍙傛暟鏃犳晥'),
    ('usage', 'smb_mgr.sh 鐢ㄦ硶涓嶅尮閰?),
    ('fail', 'smb_mgr.sh 鎶ュ憡澶辫触'),
)


def translate_failure(text: str, action: str) -> str:
    lowered = (text or '').lower()
    for needle, message in MESSAGE_HINTS:
        if needle in lowered:
            return '%s锛?s' % (action, message)
    return '%s澶辫触' % action


# ---------------------------------------------------------------------------
# 鍙戠幇鏈嶅姟韬唤锛堜富鏈哄悕 / 宸ヤ綔缁勶級
# ---------------------------------------------------------------------------

def samba_identity() -> tuple:
    """涓绘満鍚嶄笌宸ヤ綔缁勶細浼樺厛 `/etc/config/samba`锛岃涓嶅埌灏辩敤榛樿鍊笺€?""
    return (samba_config_value('name', DEFAULT_HOSTNAME),
            samba_config_value('workgroup', DEFAULT_WORKGROUP))


def samba_config_value(option: str, default: str = '') -> str:
    """鍏堣瘯 `uci -q get samba.global.<option>`锛屽啀鐩存帴瑙ｆ瀽閰嶇疆鏂囦欢銆?""
    uci = shutil.which('uci') or '/sbin/uci'
    if uci and os.path.exists(uci):
        try:
            result = run([uci, '-q', 'get', 'samba.global.%s' % option], timeout=UCI_TIMEOUT)
            value = (result.stdout or '').strip()
            if result.ok and value:
                return value
        except Error:
            pass
    return config_option(SAMBA_CONFIG, 'samba', option, default)


def config_option(path: Path, section_type: str, option: str, default: str = '') -> str:
    """浠?uci 閰嶇疆鏂囦欢閲屽彇鏌愪釜 section 鐨?option锛堟寜 section 绫诲瀷鍖归厤锛夈€?""
    try:
        # encoding 蹇呴』鏄惧紡缁欙細榛樿璧?locale锛堜腑鏂?Windows 涓婃槸 cp936锛夛紝
        # 璇诲埌 UTF-8 鐨?uci 鏂囦欢浼氭暣娈佃鏇挎崲鎴愪贡鐮侊紝瑙ｆ瀽缁撴灉姘歌繙鏄┖銆?        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return default
    for section in parse_uci(text):
        if section['type'] != section_type:
            continue
        value = section['options'].get(option)
        if value:
            return value
    return default


def hostname_valid(name) -> bool:
    """涓绘満鍚嶏紙WSD 瀹ｅ憡鍚嶏級鏄惁鍚堟硶锛?-15 浣嶃€丯etBIOS 鍙嬪ソ銆?
    姣忔潯閮戒笉婊¤冻灏辫繑鍥?False锛坄hostname_error()` 璐熻矗缁欏嚭涓枃鍘熷洜锛夈€?    """
    return HOSTNAME_PATTERN.match(str(name or '').strip()) is not None


def hostname_error(name) -> str:
    """闈炴硶涓绘満鍚嶇殑涓枃鍘熷洜锛涚┖涓茶〃绀哄悎娉曘€?""
    text = str(name or '').strip()
    if not text:
        return '璇峰～鍐欎富鏈哄悕'
    if len(text) > 15:
        return '涓绘満鍚嶆渶闀?15 涓瓧绗︼紙褰撳墠 %d 涓級锛歐indows 鐢ㄧ殑鏄?NetBIOS 鍚嶅瓧' % len(text)
    if not re.match(r'^[A-Za-z0-9]', text):
        return '涓绘満鍚嶈浠ュ瓧姣嶆垨鏁板瓧寮€澶?
    if not HOSTNAME_PATTERN.match(text):
        return '涓绘満鍚嶅彧鑳藉寘鍚瓧姣嶃€佹暟瀛椼€佺偣銆佷笅鍒掔嚎涓庣煭妯嚎'
    return ''


def validate_hostname(name) -> str:
    """鏍￠獙骞惰繑鍥炰富鏈哄悕锛涢潪娉曟椂鎶涗腑鏂?Error銆?""
    text = str(name or '').strip()
    message = hostname_error(text)
    if message:
        raise Error(message)
    return text


# ---------------------------------------------------------------------------
# 韬唤鑷姩鑾峰彇锛氱郴缁熶富鏈哄悕 / SMB(NetBIOS) 鍚?/ 灞€鍩熺綉鍦板潃 / 璁块棶鎻愮ず
# ---------------------------------------------------------------------------

def uci_option(name: str) -> str:
    """`uci -q get <name>`锛氭嬁涓嶅埌锛堟病鏈?uci 鍛戒护/娌¤繖涓厤缃級灏辫繑鍥炵┖涓诧紝涓嶆姏寮傚父銆?""
    uci = shutil.which('uci') or '/sbin/uci'
    if not uci or not os.path.exists(uci):
        return ''
    try:
        result = run([uci, '-q', 'get', name], timeout=UCI_TIMEOUT)
    except Error:
        return ''
    return (result.stdout or '').strip() if result.ok else ''


def path_gethostname() -> str:
    """绯荤粺涓绘満鍚嶏紙`hostname` 鐨勭郴缁熻皟鐢ㄧ増锛夈€?
    涓?`path_exists`/`path_isdir` 涓€鏍峰仛鎴愬彲娉ㄥ叆閽╁瓙锛氬崟鍏冩祴璇曟暣浣撴浛鎹紝
    涓嶄緷璧栬窇娴嬭瘯杩欏彴鏈哄櫒鐪熺殑鍙粈涔堝悕瀛椼€?    """
    try:
        return socket.gethostname()
    except OSError:
        return ''


def resolve_system_hostname() -> str:
    """绯荤粺涓绘満鍚嶏紙鑷姩鑾峰彇锛夛細`uci system.@system[0].hostname` 鈫?`hostname` 鈫?samba 鍚?鈫?榛樿鍊笺€?
    Windows 11 闈?WSD/mDNS/DNS 鍙戠幇璁惧锛岀敤鐨勫氨鏄郴缁熶富鏈哄悕锛汼amba 鐨?`netbios name`
    鍙奖鍝嶈€佸紡 NetBIOS 娴忚銆傚彇涓嶅埌灏卞線涓嬮€€锛岀粷涓嶆姏閿欍€?    """
    for candidate in (uci_option(SYSTEM_HOSTNAME_OPTION), path_gethostname(),
                      samba_config_value('name', '')):
        text = str(candidate or '').strip()
        if text:
            return text
    return DEFAULT_HOSTNAME


def smb_conf_netbios_name(text: str, fallback: str = '') -> str:
    """`/var/etc/smb.conf` 閲岀殑 `netbios name = xxx`锛堝彇涓嶅埌灏辩敤 `fallback`锛夈€?
    smb.conf 鐨勯敭鍚嶅ぇ灏忓啓涓嶆晱鎰燂紝鍊煎彲鑳藉甫寮曞彿锛岄兘鎸夊疄闄呭啓娉曞吋瀹广€?    """
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        key, _, value = line.partition('=')
        if key.strip().lower() == SMB_NETBIOS_OPTION:
            cleaned = _unquote(value.strip())
            if cleaned:
                return cleaned
    return str(fallback or '').strip()


def parse_ip_route_src(text: str) -> str:
    """浠?`ip route get 223.5.5.5` 鐨勮緭鍑洪噷鍙?`src <IP>`锛堝彇涓嶅埌杩斿洖绌轰覆锛夈€?""
    match = re.search(r'\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})', str(text or ''))
    return match.group(1) if match else ''


def probe_lan_address() -> str:
    """鎺㈡祴 NAS 鐨勫眬鍩熺綉鍦板潃锛氫紭鍏?`ip route get 223.5.5.5` 鐨?src锛岄€€鍒伴粯璁よ矾鐢辨帰娴嬨€?
    涓庡叾瀹冪郴缁熶氦浜掍竴鏍峰仛鎴愬彲娉ㄥ叆閽╁瓙锛堟祴璇曟暣浣撴浛鎹紝涓嶇湡鐨勬墽琛屽懡浠?寮€ socket锛夈€?    """
    ip = shutil.which('ip') or '/sbin/ip'
    if ip and os.path.exists(ip):
        try:
            result = run([ip, 'route', 'get', '223.5.5.5'], timeout=UCI_TIMEOUT)
        except Error:
            result = None
        if result is not None and result.ok:
            source = parse_ip_route_src(result.stdout)
            if source:
                return source
    try:
        return wsd.lan_address('')           # 宸叉湁鐨勯粯璁よ矾鐢辨帰娴嬶紙涓嶄細閿欐嬁 docker0锛?    except OSError:
        return ''


def access_hint(smb_name: str, address: str = '') -> str:
    """`\\\\<SMB鍚?` / `\\\\<IP>` 鐨勮闂彁绀猴紙绾嚱鏁帮紝渚夸簬鍗曟祴锛夈€?
    涓や釜鍊奸兘鏄?*鑷姩鑾峰彇**鐨勶紙姣忓彴璁惧涓嶅悓锛屼笉鑳藉啓姝伙級锛汭P 鎷夸笉鍒板氨鍙粰鍚嶅瓧閭ｄ竴鍗婏紝
    鍚嶅瓧涔熸嬁涓嶅埌锛坰mb.conf 涓?samba 閰嶇疆閮借涓嶅埌锛夊氨杩斿洖绌轰覆锛岄〉闈㈣嚜鐒朵笉鏄剧ず鎻愮ず銆?    """
    name = str(smb_name or '').strip()
    if not name:
        return ''
    address = str(address or '').strip()
    if address:
        return 'Windows 閲岀敤 \\\\%s 鎴?\\\\%s 璁块棶' % (name, address)
    return 'Windows 閲岀敤 \\\\%s 璁块棶' % name


def set_uci_option(text: str, section_type: str, option: str, value: str) -> str:
    """鎶?uci 鏂囨湰閲?*绗竴涓?*璇ョ被鍨?section 鐨?option 鏀规垚 `value`锛屽叾浣欏唴瀹瑰師鏍蜂繚鐣欍€?
    section 涓嶅瓨鍦ㄥ氨杩藉姞涓€娈碉紱option 琛屼笉瀛樺湪灏卞湪 section 澶村悗闈㈡彃涓€琛屻€傚彧鍋氭枃鏈浛鎹?    锛堢湡鏈?`/etc/config/samba` 灏辨槸杩欑绠€鍗?uci 鏂囨湰锛夛紝涓嶄緷璧?uci 鍛戒护鑳戒笉鑳?commit銆?    """
    lines = str(text or '').splitlines()
    start, end = -1, len(lines)
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped.startswith('config '):
            continue
        parts = stripped.split()
        kind = parts[1] if len(parts) > 1 else ''
        if start < 0:
            if kind == section_type:
                start = index
            continue
        end = index                                # 涓嬩竴涓?section 寮€濮?鈫?鏈鍒版涓烘
        break
    quoted = "'%s'" % str(value)
    if start < 0:                                  # 娌℃湁杩欎釜 section锛氳拷鍔犱竴娈?        block = ['config %s' % section_type, '\toption %s %s' % (option, quoted)]
        if lines and lines[-1].strip():
            block.insert(0, '')
        return '\n'.join(lines + block) + '\n'
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if not stripped.startswith(('option ', 'list ')):
            continue
        parts = stripped.split(None, 2)
        if len(parts) >= 2 and parts[1] == option:
            indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
            lines[index] = '%s%s %s %s' % (indent, parts[0], option, quoted)
            return '\n'.join(lines) + '\n'
    indent = '\t'
    lines.insert(start + 1, '%soption %s %s' % (indent, option, quoted))
    return '\n'.join(lines) + '\n'


def lan_xaddrs(address: str, port: int) -> list:
    """XAddrs 灏辨槸 Windows 鏉ュ彇璁惧鎻忚堪鐨勫湴鍧€銆?""
    if not address:
        return []
    return ['http://%s:%d/' % (address, port)]


# ---------------------------------------------------------------------------
# 鍏变韩鍛藉悕涓庤矾寰勭櫧鍚嶅崟锛堢函鍑芥暟锛?# ---------------------------------------------------------------------------

def path_exists(path) -> bool:
    """鏂囦欢绯荤粺鎺㈡祴鍋氭垚鍙敞鍏ョ殑锛氬彈绠¤矾寰勬槸 POSIX 璇箟锛屾祴璇曡鍦ㄤ换鎰忓钩鍙伴€犳矙绠便€?""
    return os.path.exists(path)


def path_isdir(path) -> bool:
    return os.path.isdir(path)


def path_realpath(path) -> str:
    return os.path.realpath(path)


def path_listdir(path) -> list:
    """鍒楃洰褰曞仛鎴愬彲娉ㄥ叆鐨勯挬瀛愶細娴嬭瘯瑕佸湪娌欑閲岄€犵洰褰曟爲锛屼笉鑳界湡鍘昏 /home銆?""
    return os.listdir(path)


def path_islink(path) -> bool:
    """鏄笉鏄鍙烽摼鎺ワ紙鍒楃洰褰曟椂瑕佹妸杞摼璺宠繃锛夈€?""
    return os.path.islink(path)


def normalize_roots(roots) -> list:
    """鍙楃璺緞涓€寰嬬敤 POSIX 璇箟瑙勮寖鍖栥€?
    鎻掍欢璺戝湪 Linux 涓婏紝鑰岃繖浜涙槸**閰嶇疆閲岀殑璺緞瀛楃涓?*锛涚敤 `os.path.normpath`
    鍦?Windows 涓婁細鎶?`/home/...` 鍙樻垚 `\\home\\...`锛岃鐧藉悕鍗曟瘮瀵瑰け鏁堬紙娴嬭瘯涔熶細鎸傦級銆?    """
    values = []
    for item in roots or ():
        text = str(item).strip()
        if text:
            values.append(posixpath.normpath(text))
    return values


def root_match(path: str, root: str) -> bool:
    """鏀寔 `/home/*/pool0/data` 杩欑鍗曞眰閫氶厤锛涘惁鍒欐寜鐩綍鍓嶇紑鍖归厤銆?""
    if '*' in root:
        pattern = '^' + re.escape(root).replace(r'\*', '[^/]+') + '$'
        return re.match(pattern, path) is not None
    return path == root or path.startswith(root.rstrip('/') + '/')


def dedupe(values) -> list:
    """鍘婚噸淇濆簭锛氱櫧鍚嶅崟/浣嶇疆鍒楄〃鐨勯『搴忓椤甸潰灞曠ず鏈夋剰涔夛紝涓嶈鐢?set 鎵撲贡銆?""
    unique: list = []
    for item in values:
        if item not in unique:
            unique.append(item)
    return unique


def under_pattern(path: str, pattern: str) -> bool:
    """`path` 鏄?`pattern` 鏈韩鎴栬惤鍦ㄥ畠涔嬩笅锛沗pattern` 鏀寔鍗曞眰 `*`锛堝 `/home/*/pool0`锛夈€?""
    regex = '^' + re.escape(str(pattern)).replace(r'\*', '[^/]+') + r'(?:/.*)?$'
    return re.match(regex, str(path)) is not None


def location_label(path) -> str:
    """寮圭獥閲屻€屼綅缃€嶅垎缁勭敤鐨勬爣绛撅紙绾嚱鏁帮紝渚夸簬鍗曟祴锛夈€?
    - `/nas/pool0` 鎴?`/home/<NAS鐢ㄦ埛>/pool0` 涔嬩笅 鈫?銆屽瓨鍌ㄦ睜銆嶏紙鍐呯疆瀛樺偍姹狅級锛?    - `/nas/mnt/usb*` 鎴?`/mnt/usb-*` 涔嬩笅 鈫?銆屽鎺ヨ澶囥€嶏紙U 鐩橈紝鍚巶鍟嗚嚜宸辩殑鎸傝浇鐐癸級锛?    - 鍏跺畠鍙栨湯娈电洰褰曞悕锛堢敤鎴疯嚜瀹氫箟 `EXTRA_ROOTS` 鏃朵篃寰楁湁涓兘鐪嬫噦鐨勬爣棰橈級銆?    """
    text = posixpath.normpath(str(path or '').strip() or '/')
    for pattern in POOL_ROOT_PATTERNS:
        if under_pattern(text, pattern):
            return '瀛樺偍姹?
    for pattern in EXTRA_ROOT_PATTERNS:
        if under_pattern(text, pattern):
            return '澶栨帴璁惧'
    return posixpath.basename(text.rstrip('/')) or text


def browse_location_label(path) -> str:
    """鐩綍娴忚鍣ㄩ噷銆屼綅缃€嶆寜閽殑鏂囧瓧锛歚/nas/mnt` 鏄剧ず鎴愩€屽鎺ュ瓨鍌ㄣ€嶃€?
    鏈鍚?`mnt` 瀵圭敤鎴锋病鎰忎箟锛屾墍浠ユ祻瑙堝脊绐楅噷鍗曠嫭瑕嗙洊涓€涓嬶紱鍏跺畠璺緞锛堝惈
    `/nas/mnt/usb` 杩欑被瀛愮洰褰曘€乣/nas/mnt/pa0`锛変粛鐒舵寜 `location_label()` 鐨勮鍒欐潵銆?    """
    text = posixpath.normpath(str(path or '').strip() or '/')
    return BROWSE_LABEL_OVERRIDES.get(text) or location_label(text)


def relative_pieces(relative) -> list:
    """鎶婃祻瑙堟帴鍙ｇ殑**鐩稿璺緞**鎷嗘垚涓€娈垫锛堢┖娈典笌 `.` 涓㈡帀锛夈€?
    `..` 涓庣粷瀵硅矾寰勭洿鎺ユ嫆缁濓細鍙楃璺緞鏄?POSIX 璇箟锛屼笉鑳界敤 `Path` 鎷?    锛圵indows 涓婁細鍙樻垚鍙嶆枩鏉狅紝鐧藉悕鍗曟瘮瀵逛笌瀛愮洰褰曞悕閮戒細閿欙級銆?    """
    text = str(relative or '').strip()
    if text.startswith('/'):
        raise Error('娴忚璺緞瑕佸～鐩稿璺緞锛堜笉瑕佷互 / 寮€澶达級')
    pieces = []
    for piece in text.split('/'):
        piece = piece.strip()
        if not piece or piece == '.':
            continue
        if piece == '..':
            raise Error('娴忚璺緞閲屼笉鍏佽鍑虹幇 ..')
        pieces.append(piece)
    return pieces


def within_roots(path: str, roots) -> bool:
    return any(root_match(path, root) for root in normalize_roots(roots))


def extra_roots() -> list:
    """`EXTRA_ROOTS` 閲?*褰撳墠鐪熷疄瀛樺湪**鐨勮拷鍔犳牴锛堝鎺ヨ澶囷級锛沗EXTRA_ROOTS=` 鏃朵负绌恒€?
    鍙繚鐣欏瓨鍦ㄧ殑锛氭嫈鐩樻椂 `/nas/mnt/usb` 涓嶅瓨鍦紝灏变笉璇ュ嚭鐜板湪鐧藉悕鍗曢噷锛堝惁鍒欓〉闈?鏍￠獙
    浼氱粰鍑轰竴涓寚鍚戠┖璺緞鐨勬牴锛夈€俙*` 鍙悆涓€灞傦紙`/mnt/usb-*`锛夛細鍒楃埗鐩綍锛屾妸鍖归厤鍒扮殑
    鐪熷疄鐩綍閫愪釜鏀惰繘鏉ャ€傝繖鏄矾寰勬帰娴嬪敮涓€鐨勫湴鏂癸紝娴嬭瘯閫氳繃鍙敞鍏ラ挬瀛?    锛坄path_isdir` / `path_listdir`锛夊湪娌欑閲岄€?U 鐩樸€?    """
    found: list = []
    for root in normalize_roots(EXTRA_ROOTS):
        if '*' in root:
            parent = posixpath.dirname(root)
            if not parent or not path_isdir(parent):
                continue
            try:
                names = path_listdir(parent)
            except OSError:
                continue
            for name in sorted(names, key=lambda item: str(item)):
                candidate = posixpath.join(parent, str(name))
                if root_match(candidate, root) and path_isdir(candidate):
                    found.append(candidate)
            continue
        if path_isdir(root):
            found.append(root)
    return dedupe(found)


def configured_extra_roots() -> list:
    """`EXTRA_ROOTS` 鐨勯厤缃€硷紙**涓?*鍒ゆ柇瀛樺湪鎬э級锛沗EXTRA_ROOTS=` 鏃朵负绌哄垪琛ㄣ€?
    寮圭獥瑕佹嬁瀹冩妸銆岄厤缃簡浣嗗綋鍓嶆病鎻掍笂銆嶇殑浣嶇疆涔熸樉绀哄嚭鏉ワ紙鏍囨垚鏈帴鍏ワ級锛?    鐧藉悕鍗曢偅杈瑰垯鐢?`extra_roots()`锛堝彧鐣欏瓨鍦ㄧ殑锛夈€?    """
    return dedupe(normalize_roots(EXTRA_ROOTS))


def data_root_for(root: Path, user_id: str) -> str:
    """`/home/u3943892/pool0/data`锛氱櫧鍚嶅崟閲岀殑鐢ㄦ埛鏁版嵁鏍癸紙鎸夊疄闄呮寕杞界偣鎺ㄥ锛夈€?""
    if not user_id:
        return ''
    return str(Path(root) / 'home' / user_id / 'pool0' / 'data')


def paths_root_for(root: Path) -> str:
    return str(Path(root) / 'nas' / 'pool0')


def derive_allowed_roots(sections, root: Path = None) -> list:
    """浠庣幇鏈夐厤缃帹瀵肩櫧鍚嶅崟锛氳处鍙风殑 `option user` + 宸叉湁鍏变韩鐨?`path` 鐨勭埗鐩綍銆?
    杩欐牱鎻掍欢鍙彲鑳芥妸鐩綍鍏变韩鍒扮敤鎴锋湰鏉ュ氨鑳藉叡浜殑鍦版柟锛堝悓涓€鎸傝浇鐐瑰唴锛夛紝
    涓嶄細鍑┖鏀捐 `/etc`銆乣/` 涔嬬被銆?    """
    root = Path(root) if root is not None else Path('/')
    values: list = []
    user_ids = []
    for section in sections.get('sambauser', []) or []:
        user_id = str(section['options'].get('user', '') or '').strip()
        if NAS_USER_PATTERN.match(user_id):
            user_ids.append(user_id)
            values.append(data_root_for(root, user_id))
    for section in sections.get('sambashare', []) or []:
        path = str(section['options'].get('path', '') or '').strip()
        if path.startswith('/'):
            parent = posixpath.dirname(posixpath.normpath(path))
            if parent and parent != '/':
                values.append(parent)
    if not user_ids:
        values.extend(str(item) for item in FALLBACK_ROOTS)
    if not any(str(item).startswith(str(Path(root) / 'nas' / 'pool0')) for item in values):
        values.append(paths_root_for(root))
    return normalize_roots(values)


def resolve_allowed_roots(sections=None, root: Path = None, extra=None) -> list:
    """鐧藉悕鍗曟渶缁堝€硷細鐜鍙橀噺鏄惧紡瑕嗙洊 > 鎸夐厤缃帹瀵?> 鍏滃簳榛樿鍊硷紝**鍐嶈拷鍔?* EXTRA_ROOTS銆?
    `ALLOWED_ROOTS` 浠嶇劧鏄?*鏄惧紡瑕嗙洊**锛堢粰浜嗗氨鍙敤瀹冿紝鐜版湁娴嬭瘯渚濊禆杩欎竴鐐癸級锛?    `EXTRA_ROOTS`锛堝鎺ヨ澶?/ U 鐩橈級鏄?*杩藉姞**鏉ユ簮锛屼袱绉嶆儏鍐典笅閮藉苟杩涙渶缁堝垪琛ㄩ噷銆?    鎶?`EXTRA_ROOTS` **鏄惧紡璁炬垚绌轰覆**灏变笉杩藉姞浠讳綍棰濆鏍癸紝姝ゆ椂 `ALLOWED_ROOTS`
    灏辨槸涓ユ牸鐨勬渶缁堢櫧鍚嶅崟锛堣繍缁村彲鐢ㄥ畠閿佹鍙叡浜寖鍥达紝瑙?parse_extra_roots()锛夈€?    `extra` 榛樿鍙?`extra_roots()`锛氬彧鍖呭惈褰撳墠鐪熷疄瀛樺湪鐨勮拷鍔犳牴锛屾嫈鐩樺悗涓嶄細鐣欎笅绌烘牴銆?    """
    if extra is None:
        extra = extra_roots()
    override = os.environ.get('ALLOWED_ROOTS', '').strip()
    if override:
        values = normalize_roots(override.split(':'))
    else:
        values = normalize_roots(FALLBACK_ROOTS)
        if CONFIG_DERIVED_ROOTS and sections:
            values.extend(derive_allowed_roots(sections, root))
        if not values:
            values = normalize_roots(FALLBACK_ROOTS)
    values.extend(normalize_roots(extra))
    return dedupe(values)


def validate_share_path(path, roots, label: str = '鐩綍') -> str:
    """鏍￠獙鍏变韩鐩綍锛氱粷瀵硅矾寰勩€佹棤 `..`銆佸湪鍏佽鐨勬牴鐩綍鍐呫€佸瓨鍦ㄤ笖鏄洰褰曘€?
    鍚屾椂鏍￠獙绗﹀彿閾炬帴瑙ｆ瀽鍚庣殑鐪熷疄璺緞锛坄/home` 涓嬬殑杞摼鏄父瑙佽秺鐣屾墜娉曪級銆?    """
    text = str(path or '').strip()
    if not text:
        raise Error('璇峰～鍐欒鍏变韩鐨?s璺緞' % label)
    if not text.startswith('/'):
        raise Error('%s蹇呴』鏄粷瀵硅矾寰勶紙浠?/ 寮€澶达級' % label)
    normalized = posixpath.normpath(text)
    if any(piece == '..' for piece in normalized.split('/')):
        raise Error('%s閲屼笉鍏佽鍑虹幇 ..' % label)
    allowed = normalize_roots(roots)
    if allowed and not within_roots(normalized, allowed):
        raise Error('璇?s涓嶅湪鍏佽鐨勬牴鐩綍鍐呫€傚厑璁哥殑鏍圭洰褰曪細%s'
                    % (label, '銆?.join(allowed)))
    # 鏂囦欢绯荤粺鎺㈡祴璧板彲娉ㄥ叆鐨勯挬瀛愶細鍙楃璺緞鏄?POSIX 璇箟锛屾祴璇曡鍦ㄤ换鎰忓钩鍙颁笂
    # 閫犳矙绠卞氨寰楄兘鏇挎崲鎺?exists/isdir/realpath銆?    if not path_exists(normalized):
        raise Error('%s涓嶅瓨鍦細%s' % (label, normalized))
    if not path_isdir(normalized):
        raise Error('%s涓嶆槸鐩綍锛?s' % (label, normalized))
    real = path_realpath(normalized)
    if allowed:
        # 绗﹀彿閾炬帴瑙ｆ瀽鍚庣殑鐪熷疄璺緞涔熷繀椤昏惤鍦ㄧ櫧鍚嶅崟锛堟垨鐢ㄦ埛涓荤洰褰曪級鍐呫€?        # 浣嗗鎺ヨ澶囨槸涓緥澶栵細`/nas/mnt/usb` 甯稿父鏄寚鍚戝巶鍟嗘寕杞界偣 `/mnt/usb-<鍝堝笇>`
        # 鐨勮蒋閾撅紝瑙ｆ瀽鍑烘潵鐨勭湡瀹炶矾寰勫ぉ鐒朵笉鍦?`/nas/mnt` 涔嬩笅锛涜€屾寕杞界偣鍚嶅瓧姣忔鎻掓嫈
        # 閮戒細鍙橈紝娌℃硶鎻愬墠鍐欒繘闈欐€佺櫧鍚嶅崟銆傛墍浠ュ鎺ヨ澶囨ā寮忥紙EXTRA_ROOT_PATTERNS锛?        # 鍗曠嫭鏀捐锛屽叾浣欒蒋閾捐秺鐣岀収鏃ф嫆缁濄€?        external = any(under_pattern(real, pattern)
                       for pattern in EXTRA_ROOT_PATTERNS)
        if not (within_roots(real, allowed)
                or real.startswith(SYMLINK_ROOT.rstrip('/') + '/')
                or external):
            raise Error('%s瑙ｆ瀽鍒扮殑鐪熷疄璺緞涓嶅湪鍏佽鑼冨洿鍐咃細%s' % (label, real))
    return normalized


def share_name_part(value: str) -> str:
    """璐﹀彿鍚嶉噷涓嶈鍑虹幇鐨勪笢瑗挎崲鎴愪笅鍒掔嚎锛岄伩鍏嶆嫾鍑哄鎬殑鍏变韩鍚嶃€?""
    cleaned = re.sub(r'[^A-Za-z0-9_.\-]', '_', str(value or '')).strip('_.-')
    return cleaned or 'user'


def plugin_share_name(account: str, taken) -> str:
    """`<璐﹀彿>_nb_<鏈€灏忓彲鐢ㄥ簭鍙?`锛涘簭鍙蜂粠 1 寮€濮嬶紝璺宠繃涓€鍒囧凡鍗犵敤鐨勫悕瀛椼€?""
    prefix = '%s%s' % (share_name_part(account), SHARE_PREFIX)
    used = {str(name) for name in (taken or ())}
    index = 1
    while '%s%d' % (prefix, index) in used:
        index += 1
    return '%s%d' % (prefix, index)


def is_plugin_share(share_name: str, account: str = '') -> bool:
    """鎻掍欢鑷缓鍏变韩锛歚<璐﹀彿>_nb_<搴忓彿>`锛涚粰璐﹀彿鏃惰姹傚墠缂€涓€鑷淬€?""
    text = str(share_name or '')
    if not text:
        return False
    if account:
        prefix = '%s%s' % (share_name_part(account), SHARE_PREFIX)
        if not text.startswith(prefix):
            return False
        text = text[len(prefix):]
        return re.match(r'^\d+$', text) is not None
    return re.search(r'(?:^|_)nb_\d+$', text) is not None


def validate_share_name(name: str) -> str:
    text = str(name or '').strip()
    if not SHARE_NAME_PATTERN.match(text):
        raise Error('鍏变韩鍚嶆棤鏁堬細鍙兘鍖呭惈瀛楁瘝銆佹暟瀛椼€佷笅鍒掔嚎銆佺偣涓庣煭妯嚎锛?-64 浣嶏級')
    return text


def validate_user_list(value, default: str = '') -> str:
    """`<user_list>` 鏄?*璐﹀彿鍚?*锛堢┖鏍煎垎闅斿涓級锛涚┖鐨勮瘽鍥為€€鍒拌鍏变韩鐨勮处鍙枫€?
    smb_mgr.sh 鍐呴儴鑷繁鍔?`samba` 鍓嶇紑锛堝疄娴嬩紶 `sambafw867` 浼氬湪 pdbedit 閭ｆ澶辫触锛夛紝
    鎵€浠ヨ繖閲屽彧鎺ュ彈璐﹀彿鍚嶏紝骞舵槑纭嫆缁?`sambaXXX` 褰㈠紡鐨勮緭鍏ャ€?    """
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = str(value or '').split()
    items = [item.strip() for item in items if str(item).strip()]
    if not items and default:
        items = [str(default)]
    cleaned = []
    for item in items:
        if not ACCOUNT_PATTERN.match(item):
            raise Error('璐﹀彿鍚嶄笉鍚堟硶锛?s锛堝彧鑳藉寘鍚瓧姣嶃€佹暟瀛椼€乢 . -锛? % item)
        if item.startswith('samba') and ACCOUNT_PATTERN.match(item[5:] or ''):
            raise Error('user_list 瑕佸～璐﹀彿鍚嶏紙濡?fw867锛夛紝涓嶈濉?SMB 璐﹀彿 %s锛?
                        'smb_mgr.sh 浼氳嚜宸卞姞 samba 鍓嶇紑' % item)
        cleaned.append(item)
    if not cleaned:
        raise Error('鑷冲皯瑕佹湁涓€涓彲璁块棶鐨勮处鍙?)
    return ' '.join(cleaned)


def validate_force_user(value) -> str:
    """`force_user` 鏄?NAS 鐢ㄦ埛鍙凤紙濡?u3943892锛夛紝涓嶆槸璐﹀彿鍚嶃€?
    椤甸潰涓婄殑鍕鹃€夋鍙戠殑鏄?`'auto'`锛堟垨绌猴級锛氫氦缁欒皟鐢ㄦ柟鍥為€€鍒?sambauser 閲岀殑 `option user`銆?    """
    text = str(value or '').strip()
    if not text or text == 'auto':
        return ''
    if not (NAS_USER_PATTERN.match(text) or ACCOUNT_PATTERN.match(text)):
        raise Error('force_user 涓嶅悎娉曪細%s' % text)
    return text


def account_to_smb_user(account: str, mapping: dict) -> str:
    """`<璐﹀彿>` 鈫?SMB 璐﹀彿锛堜粎鐢ㄤ簬**灞曠ず**锛涘懡浠よ鍙傛暟涓嶈兘鐢ㄥ畠锛夈€?""
    account = str(account or '').strip()
    if not account:
        return ''
    smb = 'samba%s' % account
    if (mapping or {}).get(smb) == account:
        return smb
    for smb_name, mapped in (mapping or {}).items():
        if mapped == account and smb_name:
            return smb_name
    return smb


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """鎶娿€屽彂鐜版湇鍔°€嶄笌銆屽叡浜洰褰曘€嶄袱鍧楄兘鍔涘寘鍦ㄤ竴璧凤紝鏂逛究 HTTP 灞備笌娴嬭瘯浣跨敤銆?""

    def __init__(self, data_dir=None, runner=None, responder_factory=None,
                 samba_mgr=None, systemctl=None, start_responder=True, on_log=None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        # 钀界洏璁剧疆锛堝紑鍏?+ 涓绘満鍚?+ 绔彛/Hello 鍛ㄦ湡锛夛紱妯″潡绾у悓鍚嶅父閲忔槸榛樿浣嶇疆
        self.state_file = self.data_dir / 'settings.json'
        self.identity_file = self.data_dir / 'wsd.json'
        self.samba_mgr = samba_mgr or SMB_MGR
        self.systemctl = systemctl or SYSTEMCTL
        # runner / responder_factory 閮藉仛鎴愬疄渚嬪睘鎬э細娴嬭瘯鍙互鏁翠綋鏇挎崲锛屼笉闇€瑕佺绯荤粺
        self.runner = runner if runner is not None else (lambda: self._run)
        self.responder_factory = responder_factory or (
            lambda **kwargs: wsd.WsdResponder(**kwargs))
        self.log_lines: list = []
        self.lock = threading.RLock()
        self.discovery: dict = {
            'running': False, 'startedAt': 0, 'msg': '', 'managed': False,
            'dropin': False, 'metadataPosts': 0, 'lastMetadataAt': 0, 'port': 0,
            'enabled': True,
        }
        self.settings = load_settings(self.state_file)
        self.hostname, self.workgroup = samba_identity()
        self.hostname = self._resolve_hostname()
        self.discovery['enabled'] = bool(self.settings.get('discoveryEnabled', True))
        self.account_cache: tuple = (0.0, None)
        self.share_cache: tuple = (0.0, None)
        # 鐧藉悕鍗曠紦瀛橈細(鏃堕棿鎴? 鍊? 褰撴椂鐨勮拷鍔犳牴鍒楄〃)鈥斺€斿鎺ヨ澶囧彲鎻掓嫈锛岃 allowed_roots()
        self.root_cache: tuple = (0.0, None, ())
        # 灞€鍩熺綉鍦板潃缂撳瓨锛?鏃堕棿鎴? 鍦板潃)鈥斺€旈〉闈㈡瘡 15 绉掓媺涓€娆＄姸鎬侊紝涓嶅繀姣忔閮藉幓鎺㈡祴
        self.address_cache: tuple = (0.0, '')
        self.on_log = on_log or (lambda message: None)
        self.responder = None
        if start_responder:
            self.start()

    # ---- 韬唤锛堜富鏈哄悕 / 宸ヤ綔缁勶級------------------------------------------
    def _resolve_hostname(self) -> str:
        """WSD 瀹ｅ憡鍚嶏細`/etc/config/samba` 鐨?`option name` > 榛樿鍊笺€?
        鎻掍欢涓嶅啀鎺ュ彈鐢ㄦ埛鎻愪緵鐨勫悕瀛楋紙涔?*涓嶅啀**寰€閰嶇疆閲屽啓鐢ㄦ埛杈撳叆鐨勫悕瀛楋級锛?        `option name` 涓庣郴缁熶富鏈哄悕涓嶄竴鑷存椂锛岄〉闈笂缁欍€屾仮澶嶄负绯荤粺涓绘満鍚嶃€嶆寜閽?        锛堣 `restore_smb_hostname()`锛夈€?        """
        return samba_config_value('name', DEFAULT_HOSTNAME)

    # ---- 鏃ュ織 -----------------------------------------------------------
    def log(self, message: str) -> None:
        line = '%s %s' % (time.strftime('%H:%M:%S'), message)
        self.log_lines.append(line)
        del self.log_lines[:-300]
        self.on_log(line)

    def recent_log(self, limit: int = 60) -> list:
        return list(self.log_lines)[-max(1, int(limit)):]

    # ---- 鍛戒护鎵ц -------------------------------------------------------
    def _run(self, command: list, timeout: int = COMMAND_TIMEOUT) -> Result:
        return run(command, timeout=timeout)

    def exec(self, command: list, timeout: int = COMMAND_TIMEOUT) -> Result:
        """缁熶竴鍑哄彛锛氫换浣曞閮ㄥ懡浠ら兘缁忚繃杩欓噷锛堜究浜庢墦妗╀笌缁熻锛夈€?""
        return self.runner()([str(item) for item in command], timeout)

    def smb(self, command: list, action: str, timeout: int = COMMAND_TIMEOUT) -> Result:
        """璺?smb_mgr.sh 骞舵妸 stdout/stderr 鍘熸枃甯﹀洖鍘伙紝澶辫触缁欎腑鏂囧師鍥犮€?""
        result = self.exec(command, timeout=timeout)
        if not result.ok:
            raise Error('%s\n%s' % (
                translate_failure('%s %s' % (result.stdout, result.stderr), action),
                describe(command, result)))
        return result

    def samba_mgr_path(self) -> str:
        if os.path.exists(self.samba_mgr):
            return self.samba_mgr
        for candidate in SMB_MGR_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        return self.samba_mgr

    # ---- 閰嶇疆璇诲彇 -------------------------------------------------------
    def _config_path(self, name: str) -> Path:
        mapping = {
            'sambauser': SAMBAUSER_CONFIG,
            'sambashare': SAMBASHARE_CONFIG,
            'samba': SAMBA_CONFIG,
            'users.map': SMB_USER_MAP,
            'smb.conf': SMB_CONF,
        }
        if name not in mapping:
            raise Error('鏈煡閰嶇疆锛?s' % name)
        return mapping[name]

    def read_config(self, name: str) -> str:
        path = self._config_path(name)
        try:
            return path.read_text(encoding='utf-8', errors='replace')
        except OSError as error:
            raise Error('璇诲彇閰嶇疆澶辫触锛?s锛?s锛? % (path, error)) from error

    def read_config_soft(self, name: str) -> str:
        """鏍稿鐢細璇讳笉鍒板氨褰撶┖锛堜笉鑳藉洜涓烘枃浠剁己浜嗗氨鎶婃搷浣滃垽澶辫触锛夈€?""
        try:
            return self._config_path(name).read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''

    def config_sections(self, name: str) -> list:
        return parse_uci(self.read_config(name))

    def user_map(self) -> dict:
        try:
            text = SMB_USER_MAP.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return {}
        return parse_smb_user_map(text)

    def samba_users(self) -> list:
        """姣忎釜璐﹀彿涓€娈点€傜湡鏈哄瓧娈碉紙NAS 瀹炴祴锛夛細

            config sambauser 'u3943892_1'
                option user 'u3943892'      鈫?NAS 鐢ㄦ埛鍙?                option name 'fw867'         鈫?璐﹀彿鍚嶏紙SMB 鐢ㄦ埛鏄?samba<璐﹀彿>锛?                list dirs '/home/u3943892/pool0/data/涓嬭浇'   鈫?鐩綍璺緞锛屼笉鏄叡浜 id

        `sambaUser` 鏁呮剰鐣欑┖锛氬畠鐢?`/etc/samba/users.map`锛坄samba<璐﹀彿> = <璐﹀彿>`锛夋垨
        `samba<璐﹀彿>` 鎺ㄥ銆?*涓嶈**鎶?`option name` 褰撴垚 SMB 璐﹀彿鈥斺€旈偅鏍蜂細璋?        `pdbedit -u fw867` 杩欑涓嶅瓨鍦ㄧ殑鐢ㄦ埛銆?        """
        accounts = []
        for section in self.config_sections('sambauser'):
            options = section['options']
            account = options.get('name') or options.get('user') or section['name']
            dirs = parse_dirs(section['lists'].get('dirs', options.get('dirs', '')))
            accounts.append({
                'section': section['name'],
                'account': account,
                'user': options.get('user', ''),
                'sambaUser': '',
                'dirs': dirs,
                'id': options.get('id', ''),
            })
        return accounts

    def samba_shares(self) -> list:
        """姣忎釜鍏变韩涓€娈点€傜湡鏈哄瓧娈碉紙NAS 瀹炴祴锛夛細

            config sambashare 'u3943892_nb_1'      鈫?娈?id 鎵嶆槸鍏变韩鏍囪瘑
                option name '鐓х墖-3943892'          鈫?璧勬簮绠＄悊鍣ㄩ噷鏄剧ず鐨勫悕瀛?                option path '/home/u3943892/pool0/data/鎴戠殑鐓х墖'
                option user_force 'u3943892'
                list users 'sambafw867'

        `smb_mgr.sh` 鐨?`add_dir`/`del_dir`銆佷互鍙娿€屾槸涓嶆槸鎻掍欢鑷繁寤虹殑銆嶉兘瑕佺敤娈?id锛?        鎵€浠ヨ繖閲?`name` = 娈?id锛屾樉绀哄悕鍗曠嫭鏀惧湪 `display`銆?        """
        shares = []
        for section in self.config_sections('sambashare'):
            options = section['options']
            users = list(section['lists'].get('users', []))
            if not users and options.get('users'):
                users = str(options['users']).split()
            shares.append({
                'name': section['name'],
                'display': options.get('name') or section['name'],
                'section': section['name'],
                'path': options.get('path', ''),
                'users': users,
                'forceUser': options.get('user_force', ''),
                'status': options.get('status', '1'),
                'comment': options.get('comment', ''),
            })
        return shares

    def allowed_roots(self, force: bool = False) -> list:
        """鐧藉悕鍗曪細鐢ㄦ埛鍦ㄩ厤缃噷鏈潵灏卞叡浜埌鐨勭洰褰曠殑鐖剁洰褰?+ 鐢ㄦ埛鏁版嵁鏍?+ /nas/pool0锛?        鍐嶈拷鍔犲鎺ヨ澶囷紙`EXTRA_ROOTS`锛孶 鐩橈級銆?
        澶栨帴璁惧鏄彲鎻掓嫈鐨勶紝鎵€浠ョ紦瀛樻妸銆屽綋鍓嶇殑杩藉姞鏍瑰垪琛ㄣ€嶄篃绠楄繘鏈夋晥鏈熷垽鏂細鎻掍笂/鎷旀帀
        U 鐩樺悗涓嬩竴娆¤皟鐢ㄥ氨鑳藉弽鏄犲嚭鏉ワ紙鏃笉鐢ㄧ瓑 30 绉掕繃鏈燂紝涔熶笉鐢ㄨ皟鐢ㄦ柟浼?force=True锛夈€?        """
        now = time.time()
        stamp, cached, cached_extra = self.root_cache
        extra = extra_roots()
        if (not force and cached is not None and now - stamp < 30
                and list(cached_extra or ()) == extra):
            return cached
        try:
            sections = {'sambauser': self.config_sections('sambauser'),
                        'sambashare': self.config_sections('sambashare')}
        except Error:
            sections = None
        values = resolve_allowed_roots(sections, extra=extra)
        self.root_cache = (now, values, extra)
        return values

    def accounts(self, force: bool = False) -> list:
        """璐﹀彿 鈫?瀹冨綋鍓嶇殑鍏变韩鐩綍锛堝惈鎻掍欢鑷缓鐨勯偅浜涳級銆?
        褰掑睘**涓嶈兘**闈?`sambauser` 鐨?`list dirs`锛氱湡鏈轰笂閭ｉ噷瀛樼殑鏄洰褰曡矾寰?        锛坄/home/u3943892/pool0/data/涓嬭浇`锛夛紝涓嶆槸鍏变韩娈?id銆傜湡姝ｇ殑鍏宠仈鏄?        `sambashare` 鐨?`list users` 閲屾湁娌℃湁 `samba<璐﹀彿>`锛堣 shares()锛夈€?        `list dirs` 鍙敤鏉ュ瀹炴姤鍛娿€岄厤缃噷鏈夌洰褰曚絾娌℃湁瀵瑰簲鍏变韩娈点€嶃€?        """
        now = time.time()
        stamp, cached = self.account_cache
        if not force and cached is not None and now - stamp < 5:
            return cached
        mapping = self.user_map()
        by_account: dict = {}
        for share in self.shares(force=True):
            if share.get('account'):
                by_account.setdefault(share['account'], []).append(share)
        result = []
        for entry in self.samba_users():
            account = entry['account']
            samba_user = entry['sambaUser'] or account_to_smb_user(account, mapping)
            items = []
            for share in by_account.get(account, []):
                items.append({
                    'name': share['name'],          # UCI 娈?id锛氬鍒犲叡浜敤鐨勫氨鏄畠
                    'display': share['display'],
                    'path': share['path'],
                    'users': share['users'],
                    'status': share['status'],
                    'forceUser': share['forceUser'],
                    'comment': share['comment'],
                    'custom': bool(share.get('custom')),
                    'missing': False,
                    'deletable': bool(share.get('custom')),
                })
            known = {item['path'].rstrip('/') for item in items}
            for path in entry['dirs']:
                if path.rstrip('/') not in known:
                    items.append({
                        'name': '', 'display': posixpath.basename(path.rstrip('/')),
                        'path': path, 'users': [], 'status': '',
                        'forceUser': '', 'comment': '閰嶇疆閲屾湁鐩綍锛屼絾娌℃湁瀵瑰簲鐨勫叡浜',
                        'custom': False, 'missing': True, 'deletable': False,
                    })
            result.append({
                'account': account,
                'user': entry['user'],
                'sambaUser': samba_user,
                'id': entry['id'],
                'shares': items,
                'customCount': sum(1 for item in items if item['custom']),
            })
        self.account_cache = (now, result)
        return result

    def shares(self, force: bool = False) -> list:
        now = time.time()
        stamp, cached = self.share_cache
        if not force and cached is not None and now - stamp < 5:
            return cached
        owners: dict = {}
        for entry in self.samba_users():
            owners.setdefault('samba%s' % entry['account'], entry['account'])
            if entry['sambaUser']:
                owners.setdefault(entry['sambaUser'], entry['account'])
            if entry['user']:
                owners.setdefault(entry['user'], entry['account'])
        result = []
        for share in self.samba_shares():
            account = ''
            for user in share['users']:
                if user in owners:
                    account = owners[user]
                    break
            if not account:
                for name, owner in owners.items():
                    if share['name'].startswith('%s_' % owner):
                        account = owner
                        break
            result.append({**share, 'account': account,
                           'custom': bool(account) and is_plugin_share(share['name'], account)})
        self.share_cache = (now, result)
        return result

    def account_names(self) -> set:
        return {item['account'] for item in self.accounts()}

    def taken_share_names(self) -> set:
        return {item['name'] for item in self.shares()}

    def account_entry(self, account: str) -> dict:
        for item in self.accounts():
            if item['account'] == account:
                return item
        raise Error('娌℃湁杩欎釜璐﹀彿锛?s' % account)

    # ---- 鐢熸晥涓庢牳瀵?-----------------------------------------------------
    def reload_command(self) -> list:
        return [self.systemctl, 'reload', *SMB_UNITS]

    def smb_service_active(self) -> bool:
        try:
            result = self.exec([self.systemctl, 'is-active', SMB_UNITS[0]], timeout=15)
        except Error:
            return False
        return (result.stdout or '').strip().startswith('active')

    def share_in_config(self, share_name: str) -> bool:
        """鏍稿鏌愪釜鍏变韩鏄惁鐪熺殑鐢熸晥浜嗐€?
        `share_name` 鏄?**UCI 娈?id**锛坄add_dir`/`del_dir` 鐢ㄧ殑灏辨槸瀹冿級銆傛牳瀵瑰垎涓ゆ锛?        1. `/etc/config/sambashare` 閲岃兘鎸夋 id 鎵惧埌杩欎竴娈碉紱
        2. `/var/etc/smb.conf` 閲屾湁瀵瑰簲鐨?section鈥斺€旀敞鎰?smb.conf 閲岀殑娈靛悕鏄?           **鏄剧ず鍚?*锛坄sambashare` 鐨?`option name`锛屽 `鐓х墖-3943892`锛夛紝
           涓嶆槸娈?id銆傜洿鎺ユ嬁娈?id 鍘绘瘮 smb.conf 浼氭案杩滀负 False锛岃 `apply_samba`
           璇姤銆屾搷浣滄湭鐢熸晥銆嶏紙鐪熸満瀹炴祴韪╄繃锛夈€?        """
        display = ''
        found = False
        for section in parse_uci(self.read_config_soft('sambashare')):
            if section['name'] == share_name:
                found = True
                display = section['options'].get('name') or section['name']
                break
        if not found:
            return False
        return display in smb_conf_sections(self.read_config_soft('smb.conf'))

    def apply_samba(self, share_name: str, expect_present: bool) -> dict:
        """`init_config` + `systemctl reload smb nmb`锛岀劧鍚庢牳瀵归厤缃噷鍒板簳鏈夋病鏈夈€?
        鍒绘剰**涓?*璋冪敤 `smb_mgr.sh reload`锛氬畠鏈€鍚庝竴姝?`systemctl restart wsdd`
        鍦ㄦ湁浜涙満鍣ㄤ笂浼氳繑鍥為潪 0锛堝嵆浣?smb.conf 鍏跺疄宸茬粡鐢熸垚濂戒簡锛夛紝
        鎷垮畠鐨勯€€鍑虹爜褰撴垚鍔熷垽瀹氬氨浼氳鎶ワ紝鎵€浠ヨ繖閲岃嚜宸辫蛋杩欎袱姝ュ苟鍥炲ご鏍稿閰嶇疆銆?        """
        commands = []
        errors = []
        result = self.exec(init_config_command(self.samba_mgr_path()), timeout=60)
        commands.append(describe(init_config_command(self.samba_mgr_path()), result))
        if not result.ok:
            errors.append(translate_failure(
                '%s %s' % (result.stdout, result.stderr), '鐢熸垚 smb.conf'))
        reload_result = self.exec(self.reload_command(), timeout=30)
        commands.append(describe(self.reload_command(), reload_result))
        if not reload_result.ok:
            # reload 澶辫触涓嶇洿鎺ュ垽姝伙細涓嬮潰杩樻湁閰嶇疆涓?is-active 涓ら亾鏍稿
            errors.append('閲嶆柊鍔犺浇 Samba 鏈嶅姟杩斿洖闈?0锛坋xit %d锛?
                          % reload_result.returncode)

        present = self.share_in_config(share_name)
        active = self.smb_service_active()
        if expect_present:
            verified = present or active
            if not verified:
                raise Error('鍏变韩 %s 娌℃湁鍑虹幇鍦?/var/etc/smb.conf 閲岋紝鎿嶄綔鏈敓鏁圽n%s'
                            % (share_name, '\n'.join(commands)))
        else:
            verified = not present
            if not verified:
                raise Error('鍏变韩 %s 浠嶇劧鍑虹幇鍦?/var/etc/smb.conf 閲岋紝鍒犻櫎鏈敓鏁圽n%s'
                            % (share_name, '\n'.join(commands)))
        return {
            'verified': verified,
            'inConfig': present,
            'smbActive': active,
            'initReturncode': result.returncode,
            'reloadReturncode': reload_result.returncode,
            'errors': errors,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'output': '\n'.join(commands),
        }

    # ---- 鍏变韩鐩綍锛氭柊澧?-------------------------------------------------
    def _taken_for(self, account: str, extra=None) -> list:
        """宸插崰鐢ㄧ殑鍏变韩鍚嶏細璐﹀彿鑷繁鐨勫叏閮ㄥ叡浜 + 鍏ㄥ眬娈?id锛堥槻姝㈣法璐﹀彿鎾炲悕锛夈€?""
        names = {str(item['name']) for item in self.shares(force=True) if item.get('name')}
        names.update(str(item) for item in self.taken_share_names())
        names.update(str(item) for item in (extra or ()))
        return sorted(names)

    def account_dir_entries(self, account) -> list:
        """璐﹀彿褰撳墠鐨勫叡浜洰褰曟潯鐩紙`accounts()` 閲屽綊鍒板畠鍚嶄笅鐨勯偅浜涳級銆?""
        return list(self.account_entry(account).get('shares') or [])

    def add_share(self, account, path, share_point='', force_user='', user_list='') -> dict:
        """缁欐煇涓处鍙峰啀鍔?*涓€涓?*鐩綍锛氭牎楠?鈫?`add_dir` 鈫?init_config + reload 鈫?鏍稿銆?
        鍗曚釜鐩綍鏃惰涓轰笌鏃х増瀹屽叏涓€鑷达紙鏍￠獙澶辫触鎴?`add_dir` 澶辫触閮芥姏涓枃 Error锛?        HTTP 灞傚洖 400锛夛紝缁撴灉閲岄櫎浜嗘柊鐨?`added/skipped/errors` 涔嬪锛屼粛甯?        `shareName/sharePoint/verified/inConfig/smbActive/reloadReturncode` 杩欎簺鑰佸瓧娈点€?        """
        result = self.add_shares(
            account, [path], share_point=share_point, force_user=force_user,
            user_list=user_list, mode='single')
        if result['added']:
            item = result['added'][0]
            if not item.get('verified'):
                # `smb_mgr.sh` 鎴愬姛浣嗛厤缃噷娌℃湁杩欎釜鍏变韩锛氳鎶ラ敊鑰屼笉鏄亣瑁呮垚鍔?                raise Error('鍏变韩 %s 娌℃湁鍑虹幇鍦?/var/etc/smb.conf 閲岋紝鎿嶄綔鏈敓鏁圽n%s'
                            % (item['shareName'], item.get('output', '')))
            return {**item, 'added': result['added'], 'skipped': result['skipped'],
                    'errors': list(item.get('errors') or []) + list(result['errors'])}
        if result['errors']:
            raise Error(result['errors'][0].get('error') or '娣诲姞鍏变韩鐩綍澶辫触')
        skipped = result['skipped'][0] if result['skipped'] else {}
        raise Error(skipped.get('error') or '娌℃湁鍙坊鍔犵殑鐩綍')

    def add_shares(self, account, paths, share_point='', force_user='', user_list='',
                   mode='batch') -> dict:
        """缁欐煇涓处鍙蜂竴娆″姞澶氫釜鐩綍銆?
        澶氱洰褰曟椂锛氬厛鍏ㄩ儴 `add_dir`锛堝崟涓け璐ュ瀹炶涓嬫潵銆佺户缁鐞嗗叾浣欙級锛岄殢鍚?        **鍙窇涓€娆?* `init_config` + `systemctl reload smb nmb`锛屽啀閫愪釜鏍稿銆?        `mode='single'` 鏃剁涓€涓洰褰曠殑瑙ｆ瀽澶辫触/宸插叡浜細鎶?Error锛堜繚鎸佹棫鎺ュ彛琛屼负锛夈€?        """
        account = str(account or '').strip()
        if not account:
            raise Error('璇烽€夋嫨瑕佹坊鍔犵洰褰曠殑璐﹀彿')
        entry = self.account_entry(account)
        # user_list 鐢ㄨ处鍙峰悕锛坰mb_mgr.sh 鑷繁鍔?samba 鍓嶇紑锛夛紱榛樿灏辨槸杩欎釜璐﹀彿
        users = validate_user_list(user_list, default=account)
        # force_user 榛樿鍙?sambauser 閲岀殑 NAS 鐢ㄦ埛鍙凤紙uXXXX锛?        force = validate_force_user(force_user or entry.get('user', ''))
        roots = self.allowed_roots()
        raw = paths if isinstance(paths, (list, tuple)) else ([] if paths in (None, '') else [paths])
        values = [str(item).strip() for item in raw if str(item or '').strip()]
        single = mode == 'single'
        if not values:
            if single:
                # 淇濇寔鏃ф帴鍙ｇ殑鎶ラ敊鎺緸锛堥〉闈㈡彁绀洪噷甯︺€岃矾寰勩€嶄袱涓瓧锛?                raise Error('璇峰～鍐欒鍏变韩鐨勭洰褰曡矾寰?)
            raise Error('璇烽€夋嫨瑕佸叡浜殑鐩綍')

        # 璐﹀彿褰撳墠宸插叡浜殑璺緞锛坄missing` 鐨勪笉绠楋細`list dirs` 閲屾湁鐩綍浣嗘病鏈夊叡浜鏃讹紝
        # 閭ｄ釜鐩綍鍏跺疄**杩樻病**鍏变韩鍑哄幓锛屾彃浠跺簲璇ヨ兘鎶婂畠鍏变韩鍑烘潵锛?        existing: dict = {}
        for share in self.account_dir_entries(account):
            if share.get('missing'):
                continue
            path = str(share.get('path', '')).rstrip('/')
            if path:
                existing.setdefault(path, share)

        wanted: list = []
        seen: set = set()
        for index, value in enumerate(values):
            try:
                directory = validate_share_path(value, roots)
            except Error as error:
                if single and index == 0:
                    raise
                wanted.append({'path': value, 'error': str(error)})
                continue
            if directory in seen:
                wanted.append({'path': directory, 'error': '鍚屼竴涓洰褰曞湪涓€娆℃彁浜ら噷鍑虹幇浜嗕袱娆?})
                continue
            if directory in existing:
                share = existing[directory]
                wanted.append({'path': directory,
                               'error': '杩欎釜鐩綍宸茬粡鏄处鍙?%s 鐨勫叡浜細%s'
                                        % (account, share.get('name', '')),
                               'shareName': share.get('name', ''),
                               'display': share.get('display', ''),
                               'shared': True})
                continue
            seen.add(directory)
            wanted.append({'path': directory})

        resolved: list = []
        problems: list = []
        for item in wanted:
            if item.get('error'):
                problems.append(item)
                continue
            try:
                resolved.append(self._prepare_share(
                    account, item['path'], share_point, resolved))
            except Error as error:
                problems.append({'path': item['path'], 'error': str(error)})
        if single and not resolved and problems:
            first = problems[0]
            raise Error(first.get('error') or '娣诲姞鍏变韩鐩綍澶辫触')
        result = self._run_add_batch(account, resolved, users, force, skipped=problems)
        if single and not result['added']:            # `add_dir` 澶辫触锛氭棫鎺ュ彛鐢?400 鎶ラ敊
            first = result['errors'][0] if result['errors'] else {}
            raise Error(first.get('error') or '娣诲姞鍏变韩鐩綍澶辫触')
        return result

    def _prepare_share(self, account: str, directory: str, share_point, resolved) -> dict:
        """鍒嗛厤鍏变韩鍚嶄笌鏄剧ず鍚嶏紙鏄剧ず鍚嶆病缁欏氨鐢ㄧ洰褰曞悕锛屼笌鏃ц涓轰竴鑷达級銆?""
        taken = self._taken_for(account, [item['shareName'] for item in resolved])
        share_name = plugin_share_name(account, taken)
        if share_name in taken:
            raise Error('鍏变韩鍚?%s 宸茶鍗犵敤锛堟湰涓嶈鍙戠敓锛岃鍒锋柊鍚庨噸璇曪級' % share_name)
        point = validate_share_name(share_point) if str(share_point or '').strip() else \
            (posixpath.basename(directory.rstrip('/')) or share_name)
        return {'path': directory, 'shareName': share_name, 'sharePoint': point}

    def _run_add_batch(self, account: str, resolved: list, users: str, force: str,
                       skipped=None) -> dict:
        """`add_dir` 脳 N 鈫?涓€娆?init_config + reload 鈫?閫愪釜鏍稿銆?
        涓€涓兘娌″緱鍔狅紙鍏ㄩ儴宸插叡浜?鏍￠獙澶辫触锛夋椂涓嶅啀璺戞敹灏惧懡浠わ細娌℃湁鏀瑰姩灏变笉璇?reload銆?        """
        self.account_cache = (0.0, None)
        self.share_cache = (0.0, None)
        added: list = []
        errors: list = [dict(item) for item in (skipped or [])]
        if not resolved:
            return {'added': added, 'skipped': [dict(item) for item in (skipped or [])],
                    'errors': errors, 'account': account, 'apply': None}
        for item in resolved:
            directory, share_name = item['path'], item['shareName']
            point = item['sharePoint']
            command = add_dir_command(share_name, directory, users, point, force,
                                      self.samba_mgr_path())
            try:
                result = self.smb(command, '娣诲姞鍏变韩鐩綍')
            except Error as error:
                # 浠讳竴鐩綍澶辫触瑕佸瀹炴姤鍑烘潵锛屽苟缁х画澶勭悊鍏朵綑鐩綍
                errors.append({'path': directory, 'shareName': share_name,
                               'error': str(error)})
                continue
            self.log('鏂板鍏变韩 %s 鈫?%s锛堣处鍙?%s锛宖orce_user %s锛?
                     % (share_name, directory, users, force or '鈥?))
            added.append({
                'shareName': share_name,
                'sharePoint': point,
                'account': account,
                'path': directory,
                'users': users.split(),
                'forceUser': force,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'command': describe(command, result),
            })
        applied = self.apply_samba_batch([item['shareName'] for item in added])
        for item in added:
            state = applied['shares'].get(item['shareName'], {'present': False})
            item_errors = list(applied['errors'])
            if not state.get('present'):
                item_errors.append('鍏变韩 %s 娌℃湁鍑虹幇鍦?/var/etc/smb.conf 閲岋紝鎿嶄綔鏈敓鏁圽n%s'
                                   % (item['shareName'], applied['output']))
                errors.append({'path': item['path'], 'shareName': item['shareName'],
                               'error': item_errors[-1]})
            item.update({
                'reloaded': applied['reloadReturncode'] == 0 or bool(state.get('present')),
                'verified': bool(state.get('present')),
                'inConfig': bool(state.get('present')),
                'smbActive': applied['smbActive'],
                'initReturncode': applied['initReturncode'],
                'reloadReturncode': applied['reloadReturncode'],
                'errors': item_errors,
                'output': item['command'] + '\n' + applied['output'],
            })
        return {'added': added, 'skipped': [dict(item) for item in (skipped or [])],
                'errors': errors, 'account': account, 'apply': applied}

    def apply_samba_batch(self, share_names, expect_present: bool = True) -> dict:
        """`init_config` + `systemctl reload smb nmb` **涓€娆?*锛岀劧鍚庢牳瀵规瘡涓叡浜€?
        涓?`apply_samba` 鐨勫尯鍒彧鏈変竴涓細鎵归噺澧炲垹鏃跺彧璺戜竴娆℃敹灏惧懡浠ゃ€?
        `expect_present=True`锛堥粯璁わ紝鏂板璺緞锛岃涓轰笌浠ュ墠瀹屽叏涓€鑷达級锛歚verified` 鏄?        銆岄厤缃噷鏈夎繖涓銆佹垨鑰呮湇鍔＄‘瀹炴槸 active銆嶏紱`inConfig` 琛ㄧず銆屽叏閮藉湪閰嶇疆閲屻€嶃€?        `expect_present=False`锛堟壒閲忓垹闄わ級锛氬垽瀹氬弽杩囨潵锛宍verified` 鏄€岄厤缃噷**娌℃湁**
        杩欎釜娈点€嶏紝`inConfig` 琛ㄧず銆屽叏閮戒笉鍦ㄩ厤缃噷銆嶃€?        """
        names = [str(item) for item in (share_names or []) if str(item or '').strip()]
        commands = []
        errors = []
        result = self.exec(init_config_command(self.samba_mgr_path()), timeout=60)
        commands.append(describe(init_config_command(self.samba_mgr_path()), result))
        if not result.ok:
            errors.append(translate_failure(
                '%s %s' % (result.stdout, result.stderr), '鐢熸垚 smb.conf'))
        reload_result = self.exec(self.reload_command(), timeout=30)
        commands.append(describe(self.reload_command(), reload_result))
        if not reload_result.ok:
            errors.append('閲嶆柊鍔犺浇 Samba 鏈嶅姟杩斿洖闈?0锛坋xit %d锛? % reload_result.returncode)
        present = {name: self.share_in_config(name) for name in names}
        shares: dict = {}
        for name in names:
            found = bool(present[name])
            shares[name] = {'present': found,
                            'verified': found if expect_present else not found}
        if expect_present:
            # 涓?`apply_samba` 涓€鑷达細鍙湁閰嶇疆閲岀湡鐨勭己娈垫椂鎵嶅幓闂湇鍔＄姸鎬?            active = self.smb_service_active() if not all(present.values()) else False
            for name in names:
                shares[name]['verified'] = bool(present[name]) or active
            in_config = all(present.values()) if names else True
        else:
            # 鍒犻櫎璺緞涓嶅幓闂?is-active锛氭宸蹭粠閰嶇疆閲屾秷澶辨湰韬氨鏄瘉鎹?            active = False
            in_config = all(not value for value in present.values()) if names else False
        return {
            'shares': shares,
            'inConfig': in_config,
            'smbActive': active,
            'initReturncode': result.returncode,
            'reloadReturncode': reload_result.returncode,
            'errors': errors,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'output': '\n'.join(commands),
        }

    # ---- 璐﹀彿鏁版嵁鐩綍涓庣洰褰曟祻瑙?-----------------------------------------
    def account_data_root(self, account: str) -> str:
        """璐﹀彿鐨勬暟鎹牴鐩綍锛歚<璐﹀彿>` 鈫?`sambauser` 鐨?`option user` 鈫?`/home/<uXXXX>/pool0/data`銆?
        鎷夸笉鍒?NAS 鐢ㄦ埛鍙锋椂锛岄€€鍥炲厑璁告牴鐩綍閲岀涓€鏉?`/home/...` 褰㈡€佺殑銆?        """
        account = str(account or '').strip()
        if not account:
            raise Error('璇烽€夋嫨璐﹀彿')
        entry = self.account_entry(account)
        user_id = str(entry.get('user', '') or '').strip()
        if user_id:
            # 鐪熸満甯冨眬灏辨槸 `/home/<NAS鐢ㄦ埛>/pool0/data`锛涜繖閲岀洿鎺ユ寜 POSIX 鎷煎瓧绗︿覆
            # 锛堜笉瑕佺敤 `Path`锛屽畠鍦?Windows 涓婁細鎷煎嚭鍙嶆枩鏉狅紝鐧藉悕鍗曟瘮瀵逛細澶辨晥锛?            canonical = '/home/%s/pool0/data' % user_id
            for candidate in (canonical, data_root_for(Path('/'), user_id)):
                if candidate and path_isdir(candidate):
                    return posixpath.normpath(candidate)
            return posixpath.normpath(canonical)
        for root in self.allowed_roots():
            if root.startswith('/home/') and '*' not in root:
                return root
        return ''

    def _browse_dirs(self, root: str, account: str) -> list:
        """鏌愪釜鏍圭洰褰曚笅鐨?*涓€灞?*瀛愮洰褰曪紙鍙垪鐩綍锛岃烦杩囬殣钘忕洰褰曚笌绗﹀彿閾炬帴锛夛紝鏍囨敞宸插叡浜€?
        `browse_account_dirs` 鐨勩€屽瓨鍌ㄦ睜銆嶄笌銆屽鎺ヨ澶囥€嶄袱涓綅缃叡鐢ㄨ繖涓€浠介€昏緫锛?        涓よ竟鐨?`shared`/`shareName`/`display`/`deletable` 瀛楁鎵嶄細瀹屽叏涓€鑷淬€?        """
        entries = []
        for name in sorted(path_listdir(root), key=lambda item: str(item)):
            text = str(name)
            if not text or text.startswith('.'):
                continue
            path = posixpath.join(root, text)
            if path_islink(path):
                continue                        # 绗﹀彿閾炬帴涓€寰嬩笉鍒楋紙鍙兘鏄秺鐣岀殑杞摼锛?            if not path_isdir(path):
                continue
            entries.append((text, path))
        known: dict = {}
        for share in self.account_dir_entries(account):
            if share.get('missing'):
                # `list dirs` 閲屾湁鐩綍浣嗘病鏈夊叡浜锛氬畠鍏跺疄**杩樻病**鍏变韩鍑哄幓锛?                # 寮圭獥閲岃鑳藉嬀閫夛紙鍚﹀垯鐢ㄦ埛姘歌繙娌℃硶鎶婅繖涓洰褰曞叡浜嚭鏉ワ級銆?                continue
            path = str(share.get('path', '')).rstrip('/')
            if path:
                known.setdefault(path, share)
        dirs = []
        for name, path in entries:
            share = known.get(path.rstrip('/'))
            dirs.append({
                'name': name,
                'path': path,
                'shared': share is not None,
                'shareName': (share or {}).get('name', ''),
                'display': (share or {}).get('display', ''),
                # 鑳戒笉鑳界敱鎻掍欢鍒犳帀锛堝脊绐楅噷鍐冲畾杩欎釜鍕鹃€夋鍙笉鍙互鍙栨秷锛夛細
                # 鍙湁鎻掍欢鑷繁寤虹殑 `<璐﹀彿>_nb_<搴忓彿>` 鎵嶄负 True锛涘畼鏂?App 寤虹殑閭ｆ潯
                # 涓庢湭鍏变韩鐨勭洰褰曢兘鏄?False銆?                'deletable': bool((share or {}).get('deletable')),
            })
        return dirs

    def browse_account_dirs(self, account: str) -> dict:
        """璐﹀彿鏁版嵁鏍圭洰褰曚笅鐨?*瀛愮洰褰?*鍒楄〃锛堝彧鍒楃洰褰曪紝璺宠繃闅愯棌鐩綍涓庣鍙烽摼鎺ワ級銆?
        `dirs` 淇濇寔鏃х粨鏋勶紙鑰佸墠绔?鑰佹祴璇曠洿鎺ョ敤锛夛紝鍙﹀缁?`locations`锛氬脊绐楁寜銆屼綅缃€?        鍒嗙粍娓叉煋鈥斺€斿唴缃瓨鍌ㄦ睜锛堣处鍙锋暟鎹牴锛? 澶栨帴璁惧锛坄EXTRA_ROOTS`锛岄粯璁?`/nas/mnt`锛夈€?        澶栨帴璁惧鎷旀帀鏃?`available` 涓?False銆乣dirs` 涓虹┖锛屽脊绐楁樉绀恒€屾湭鎺ュ叆銆嶏紱
        杩欎笉鏄敊璇紙宸插叡浜殑 U 鐩樼洰褰曟殏鏃朵笉鍙敤灞炰簬姝ｅ父鎯呭喌锛夈€?        """
        account = str(account or '').strip()
        root = self.account_data_root(account)
        if not root:
            raise Error('娌℃湁鎵惧埌璐﹀彿 %s 鐨勬暟鎹洰褰曪紙/etc/config/sambauser 閲屾病鏈夊彲鐢ㄧ殑鐢ㄦ埛鍙凤級'
                        % account)
        if not path_exists(root):
            raise Error('鏁版嵁鐩綍涓嶅瓨鍦細%s' % root)
        if not path_isdir(root):
            raise Error('鏁版嵁鐩綍涓嶆槸涓€涓洰褰曪細%s' % root)
        dirs = self._browse_dirs(root, account)
        locations = [{'label': location_label(root), 'root': root,
                      'available': True, 'dirs': dirs}]
        present = extra_roots()
        for extra in self.extra_location_paths():
            if extra == root:                       # 鏋佺閰嶇疆涓嬩笌瀛樺偍姹犻噸澶嶇殑鏍逛笉閲嶅鍒?                continue
            available = extra in present
            extra_dirs: list = []
            if available:
                try:
                    extra_dirs = self._browse_dirs(extra, account)
                except OSError as error:
                    # 鎸傝浇鐐规帀浜?娌℃潈闄愶細涓嶈兘鎶婃暣涓脊绐楁嫋鍨紙瀛樺偍姹犻偅浠界収鏃ц缁欏嚭鏉ワ級锛?                    # 浣嶇疆鏍囨垚涓嶅彲鐢紝鐢ㄦ埛杩樿兘鎵嬪～鍒殑璺緞銆?                    self.log('璇诲彇 %s 澶辫触锛?s' % (extra, error))
                    available = False
            locations.append({
                'label': location_label(extra),
                'root': extra,
                'available': available,
                'dirs': extra_dirs,
            })
        return {'ok': True, 'account': account, 'root': root, 'dirs': dirs,
                'locations': locations}

    # ---- 鐩綍娴忚锛堛€屾坊鍔犲叡浜€嶅脊绐楋紝浜や簰涓?transmission 鎻掍欢涓€鑷达級--------
    def extra_location_paths(self) -> list:
        """杩藉姞鏍癸紙澶栨帴瀛樺偍锛夌殑灞曠ず/娴忚椤哄簭銆?
        閫氶厤锛坄/mnt/usb-*`锛夊睍寮€鎴愬綋鍓嶅瓨鍦ㄧ殑鐪熷疄鎸傝浇鐐癸紝浣嶇疆灏卞湪閰嶇疆椤哄簭涓婏紱
        閰嶇疆浜嗕絾褰撳墠涓嶅瓨鍦ㄧ殑锛堥潪閫氶厤锛変篃淇濈暀锛屽脊绐楁妸瀹冩爣鎴愩€屾湭鎺ュ叆銆嶃€?        """
        present = extra_roots()
        values: list = []
        for item in configured_extra_roots():
            if '*' in item:
                for entry in present:
                    if entry not in values and root_match(entry, item):
                        values.append(entry)
                continue
            if item not in values:
                values.append(item)
        for entry in present:                   # 鍏滃簳锛歱resent 閲屽叾瀹冩潵婧愮殑鏍逛篃鍒紡
            if entry not in values:
                values.append(entry)
        return values

    def share_locations(self, account: str) -> list:
        """寮圭獥閲岀殑銆屼綅缃€嶅垪琛細瀛樺偍姹狅紙璐﹀彿鏁版嵁鏍癸級+ `EXTRA_ROOTS`锛堥粯璁ゅ鎺ュ瓨鍌級銆?
        褰㈠ `{'index': 0, 'label': '瀛樺偍姹?, 'path': '/home/u3943892/pool0/data',
        'exists': True}`锛沗/nas/mnt` 鐨勬爣棰樻槸銆屽鎺ュ瓨鍌ㄣ€嶏紙瑙?browse_location_label锛夛紝
        鎷旂洏/涓嶅瓨鍦ㄦ椂 `exists` 涓?False锛堝脊绐楁爣銆屾湭鎺ュ叆銆嶅苟绂佺敤杩涘叆锛夈€?        """
        account = str(account or '').strip()
        root = self.account_data_root(account)          # 璐﹀彿涓嶅鏃惰繖閲屾姏涓枃 Error
        locations = [{'index': 0, 'label': location_label(root), 'path': root,
                      'exists': bool(path_isdir(root))}]
        for item in self.extra_location_paths():
            if item == root:                            # 鏋佺閰嶇疆涓嬬殑閲嶅鏍逛笉閲嶅鍒?                continue
            locations.append({'index': len(locations), 'label': browse_location_label(item),
                              'path': item, 'exists': bool(path_isdir(item))})
        return locations

    def browse_share_dirs(self, account: str, root_index=0, relative: str = '') -> dict:
        """閫愬眰娴忚鏌愪釜銆屼綅缃€嶄笅鐨勫瓙鐩綍锛堛€屾坊鍔犲叡浜€嶅脊绐楃殑鍚庣锛夈€?
        - `root_index`锛氫綅缃簭鍙凤紙瑙?`share_locations()`锛夛紝榛樿 0 = 瀛樺偍姹狅紱
        - `relative`锛氱浉瀵硅浣嶇疆鐨勮矾寰勶紝绌轰覆 = 浣嶇疆鏍圭洰褰曟湰韬紱
        - 鍙垪**涓€灞?*瀛愮洰褰曪紝璺宠繃闅愯棌鐩綍涓庣鍙烽摼鎺ワ紱瓒婄晫锛坄..`/缁濆璺緞锛夈€?          浣嶇疆涓嶅瓨鍦ㄣ€佺洰褰曚笉瀛樺湪閮芥姏涓枃 Error锛圚TTP 灞傚洖 400锛夈€?
        杩斿洖閲屽悓鏃跺甫涓?`locations`锛屽脊绐椾竴娆¤姹傚氨鑳界敾鍑轰綅缃垏鎹笌褰撳墠浣嶇疆锛?        `path` 鏄鑼冨寲鍚庣殑鐩稿璺緞锛堢粰銆屼笂涓€绾с€嶇敤锛夛紝`absolute` 鏄畬鏁寸粷瀵硅矾寰勩€?        """
        account = str(account or '').strip()
        locations = self.share_locations(account)
        try:
            index = int(root_index)
        except (TypeError, ValueError) as error:
            raise Error('浣嶇疆鏃犳晥锛?s' % (root_index,)) from error
        if not 0 <= index < len(locations):
            raise Error('浣嶇疆鏃犳晥锛?s' % index)
        entry = locations[index]
        base = entry['path']
        pieces = relative_pieces(relative)
        clean = '/'.join(pieces)
        target = posixpath.join(base, *pieces) if pieces else base
        # 鐩稿璺緞宸茬粡鎷掔粷 `..` 涓庣粷瀵硅矾寰勶紝杩欓噷鍐嶇‘璁や竴娆＄粨鏋滅‘瀹炶惤鍦ㄨ繖涓綅缃噷
        if not within_roots(target, [base]):
            raise Error('鐩綍涓嶅湪杩欎釜浣嶇疆閲岋細%s' % target)
        if pieces and not entry['exists']:
            raise Error('杩欎釜浣嶇疆褰撳墠涓嶅彲鐢紙鏈帴鍏ワ級锛?s' % base)
        if not path_exists(target):
            raise Error('鐩綍涓嶅瓨鍦細%s' % target)
        if not path_isdir(target):
            raise Error('涓嶆槸涓€涓洰褰曪細%s' % target)
        known: dict = {}
        for share in self.account_dir_entries(account):
            if share.get('missing'):
                continue
            path = str(share.get('path', '')).rstrip('/')
            if path:
                known.setdefault(path, share)
        items = []
        for name in sorted(path_listdir(target), key=lambda item: str(item)):
            text = str(name)
            if not text or text.startswith('.'):
                continue
            child = posixpath.join(target, text)
            if path_islink(child):
                continue                        # 绗﹀彿閾炬帴涓€寰嬩笉鍒楋紙鍙兘鏄秺鐣岀殑杞摼锛?            if not path_isdir(child):
                continue
            items.append({
                'name': text,
                'path': '/'.join([clean, text]) if clean else text,   # 鐩稿璺緞
                'absolute': child,                                     # 瀹屾暣缁濆璺緞
                'shared': child.rstrip('/') in known,                  # 宸茬粡鍏变韩杩囦簡
            })
        return {'ok': True, 'account': account, 'root': index, 'path': clean,
                'absolute': target, 'exists': bool(entry['exists']),
                'items': items, 'locations': locations}

    # ---- 鍏变韩鐩綍锛氬垹闄?-------------------------------------------------
    def _deletable_share(self, share_name) -> dict:
        """銆岃繖涓叡浜兘涓嶈兘鍒犮€嶇殑鏍￠獙锛氳兘鍒犲氨杩斿洖瀹冪殑閰嶇疆鏉＄洰锛屽惁鍒欐姏涓枃 Error銆?
        `delete_share` 涓?`delete_shares` 鍏辩敤锛屾姤閿欐枃妗堝畬鍏ㄤ竴鑷达細
        绌哄悕 / 绯荤粺鍏变韩 / 鎵句笉鍒?/ 涓嶆槸鎻掍欢鑷缓鐨?`<璐﹀彿>_nb_<搴忓彿>`銆?        """
        share_name = str(share_name or '').strip()
        if not share_name:
            raise Error('璇烽€夋嫨瑕佸垹闄ょ殑鍏变韩')
        if share_name in PROTECTED_SHARES:
            raise Error('绯荤粺鍏变韩 %s 涓嶅厑璁稿垹闄? % share_name)
        share = next((item for item in self.shares() if item['name'] == share_name), None)
        if share is None:
            raise Error('娌℃湁鎵惧埌鍏变韩锛?s' % share_name)
        account = share.get('account', '')
        if not account or not is_plugin_share(share_name, account):
            raise Error('鍙厑璁稿垹闄ゆ彃浠惰嚜宸辨坊鍔犵殑鍏变韩锛?璐﹀彿>_nb_<搴忓彿>锛夛紱'
                        '%s 鐢辩郴缁熸垨灏忕背 App 绠＄悊' % share_name)
        return share

    def _clear_share_caches(self) -> None:
        self.account_cache = (0.0, None)
        self.share_cache = (0.0, None)

    def delete_share(self, share_name) -> dict:
        """鍙厑璁稿垹鎻掍欢鑷繁寤虹殑锛歚<璐﹀彿>_nb_<搴忓彿>`銆?""
        share = self._deletable_share(share_name)
        name = str(share_name).strip()
        command = del_dir_command(name, self.samba_mgr_path())
        result = self.smb(command, '鍒犻櫎鍏变韩鐩綍')
        self.log('鍒犻櫎鍏变韩 %s' % name)
        applied = self.apply_samba(name, expect_present=False)
        self._clear_share_caches()
        return {
            'shareName': name,
            'account': share.get('account', ''),
            'path': share.get('path', ''),
            'reloaded': applied['reloadReturncode'] == 0 or applied['verified'],
            'stdout': result.stdout,
            'stderr': result.stderr,
            'output': describe(command, result) + '\n' + applied['output'],
            **{key: applied[key] for key in
               ('verified', 'inConfig', 'smbActive', 'reloadReturncode', 'errors')},
        }

    def delete_shares(self, share_names) -> dict:
        """涓€娆″垹澶氫釜鎻掍欢鑷缓鍏变韩锛歚del_dir` 脳 N 鈫?**鍙窇涓€娆?* init_config + reload銆?
        璇箟涓?`delete_share` 瀹屽叏涓€鑷达紙鍚屾牱鐨勬牎楠屻€佸悓鏍风殑鎶ラ敊鏂囨锛夛紝鍖哄埆鍙湁涓€涓細
        鏀跺熬鍛戒护鍙窇涓€娆°€傚厛**鍏ㄩ儴**鏍￠獙閫氳繃鍐嶅姩鎵嬶細鍒楄〃閲屽彧瑕佹湁涓€鏉′笉鍚堟硶
        锛堝彈淇濇姢鐨?/ 鎵句笉鍒扮殑 / 瀹樻柟 App 寤虹殑锛夛紝涓€鏉?`del_dir` 閮戒笉浼氭墽琛屻€?        """
        raw = share_names if isinstance(share_names, (list, tuple)) else \
            ([] if share_names in (None, '') else [share_names])
        names: list = []
        for item in raw:
            text = str(item or '').strip()
            if text and text not in names:              # 鍘婚噸 + 蹇界暐绌哄€?                names.append(text)
        if not names:
            raise Error('璇烽€夋嫨瑕佸垹闄ょ殑鍏变韩')

        # 绗竴閬嶏細鍏ㄩ儴鏍￠獙锛堜换浣曚竴鏉′笉鍚堟硶閮藉湪杩欓噷鎶涢敊锛屾鏃惰繕娌¤窇杩囦换浣曞懡浠わ級
        shares = [self._deletable_share(name) for name in names]

        removed: list = []
        for share, name in zip(shares, names):
            command = del_dir_command(name, self.samba_mgr_path())
            result = self.smb(command, '鍒犻櫎鍏变韩鐩綍')
            self.log('鍒犻櫎鍏变韩 %s' % name)
            removed.append({
                'shareName': name,
                'account': share.get('account', ''),
                'path': share.get('path', ''),
                'inConfig': True,                       # 涓嬮潰鎸夋牳瀵圭粨鏋滄敼鍐?                'verified': False,
                'command': describe(command, result),
                'stdout': result.stdout,
                'stderr': result.stderr,
            })

        applied = self.apply_samba_batch(names, expect_present=False)
        for item in removed:
            state = applied['shares'].get(item['shareName'], {})
            item['inConfig'] = bool(state.get('present'))
            item['verified'] = bool(state.get('verified'))
        self._clear_share_caches()

        errors = list(applied['errors'])
        for item in removed:
            if not item['verified']:
                errors.append('鍏变韩 %s 浠嶇劧鍑虹幇鍦?/var/etc/smb.conf 閲岋紝鍒犻櫎鏈敓鏁?
                              % item['shareName'])
        return {
            'removed': removed,
            'verified': all(item['verified'] for item in removed),
            'errors': list(dict.fromkeys(errors)),
            'initReturncode': applied['initReturncode'],
            'reloadReturncode': applied['reloadReturncode'],
            'output': applied['output'],
        }

    # ---- 瀹樻柟 wsdd 鎺ョ -------------------------------------------------
    def systemctl_command(self, *args) -> list:
        return [self.systemctl, *args]

    def unit_state(self, unit: str = '') -> str:
        """`systemctl show -p UnitFileState`锛氳瘖鏂敤锛堜笉鍙備笌鎺ョ鍒ゅ畾锛夈€?""
        result = self.exec(self.systemctl_command(
            'show', '-p', 'UnitFileState', '--value', unit or WSDD_UNIT))
        return (result.stdout or '').strip()

    def kill_wsdd_processes(self) -> str:
        """鍏滃簳鏉€鎺夋畫鐣欑殑 wsdd 杩涚▼銆?
        鐪熸満锛?92.168.1.8锛?*娌℃湁 pkill**锛坄pkill: 鏈壘鍒板懡浠锛宔xit 127锛夛紝鍙湁
        `killall`鈥斺€斿畼鏂?`/usr/bin/wsd` 鍖呰鑴氭湰鐢ㄧ殑涔熸槸 `killall wsdd`銆傛墍浠ユ寜
        `KILL_CANDIDATES` 椤哄簭鎺㈡祴瀹為檯瀛樺湪鐨勯偅涓紱閮芥病鏈夊氨璺宠繃锛屽彧渚濊禆 systemctl銆?        娌℃湁杩涚▼鏃?killall/pkill 閮戒細杩斿洖闈?0锛岃繖涓嶇畻澶辫触銆?        """
        candidates = [KILL] if KILL else []
        for name in KILL_CANDIDATES:
            found = shutil.which(name)
            if found and found not in candidates:
                candidates.append(found)
        if not candidates:
            return '绯荤粺閲屾病鏈?killall/pkill锛岃烦杩囧厹搴曟竻鐞嗭紙鍙緷璧?systemctl stop锛?
        for path in candidates:
            name = os.path.basename(path)
            argv = [path, WSDD_UNIT] if name == 'killall' else [path, '-x', WSDD_UNIT]
            try:
                result = self.exec(argv, timeout=15)
            except Error as error:
                return '鍏滃簳娓呯悊澶辫触锛?s锛夛細%s' % (name, error)
            if result.ok:
                return '宸叉竻鐞嗘畫鐣欑殑 wsdd 杩涚▼锛?s锛? % name
            return '娌℃湁闇€瑕佹竻鐞嗙殑 wsdd 杩涚▼锛?s锛? % name
        return '鍏滃簳娓呯悊鏈墽琛?

    def wsdd_takeover(self) -> str:
        """璁╁畼鏂?wsdd 涓嶅啀鎶?3702锛氬仠鏈嶅姟 鈫?鍏滃簳娓呯悊 鈫?鍐欑┖鎿嶄綔 drop-in 鈫?daemon-reload銆?
        鐢?drop-in 鑰屼笉鏄?`systemctl mask`锛歮ask 涔嬪悗瀹樻柟 `smb_mgr.sh` 鏀跺熬閭ｅ彞
        `systemctl restart wsdd` 浼氬け璐ュ苟璁╂暣鏉″懡浠よ繑鍥?1锛岀敤鎴峰湪 App 閲屽氨浼氱湅鍒?        銆屼繚瀛樺叡浜け璐ャ€嶏紙铏界劧 smb.conf 宸茬粡鍐欏ソ浜嗭級銆俤rop-in 鎶婂畼鏂瑰崟鍏冨彉鎴愮┖鎿嶄綔锛?        `restart` 渚濇棫杩斿洖 0锛屽畼鏂瑰姛鑳芥病鏈夊彲瑙佸洖褰掋€?
        鐪熸満涓婅俯杩囩殑涓や釜鍧戯紝鍒啀鏀瑰洖鍘伙細
        - 鍋滄閭ｆ蹇呴』甯?`systemctl` 鍓嶇紑銆傛紡鎺変細鎵ц鎴?`stop wsdd`锛屾棩蹇楅噷鏄?          銆屽仠姝㈠畼鏂?wsdd澶辫触锛氭壘涓嶅埌鍛戒护 stop銆嶏紝缁撴灉鏄畼鏂规湇鍔′竴鐩村崰鐫€ 3702銆?        - 鍋滄湇鏃?`ExecStopPost --restore-wsdd` 浼氭妸瀹樻柟 wsdd 鎷夊洖鏉ワ紝涓庝笅涓€娆″惎鍔ㄧ殑
          鎺ョ瀛樺湪绔炴€侊紝鎵€浠ュ仠瀹岃**鑷 3702 骞堕噸璇曚竴娆?*銆?        """
        messages = []
        for attempt in (1, 2):
            try:
                result = self.exec(self.systemctl_command('stop', WSDD_UNIT), timeout=30)
                messages.append('鍋滄瀹樻柟 wsdd' + (
                    '鎴愬姛' if result.ok else '杩斿洖闈?0锛坋xit %d锛夛細%s'
                    % (result.returncode, (result.stderr or result.stdout).strip())))
            except Error as error:
                messages.append('鍋滄瀹樻柟 wsdd澶辫触锛?s' % error)
            messages.append(self.kill_wsdd_processes())

            try:
                WSDD_DROPIN.parent.mkdir(parents=True, exist_ok=True)
                WSDD_DROPIN.write_text(WSDD_DROPIN_TEXT, encoding='utf-8', newline='\n')
                messages.append('宸插啓鍏ョ┖鎿嶄綔 drop-in锛?s' % WSDD_DROPIN)
            except OSError as error:
                messages.append('鍐欏叆 drop-in 澶辫触锛?s' % error)

            try:
                result = self.exec(self.systemctl_command('daemon-reload'), timeout=30)
                messages.append('daemon-reload' + (
                    '鎴愬姛' if result.ok else '杩斿洖闈?0锛坋xit %d锛? % result.returncode))
            except Error as error:
                messages.append('daemon-reload 澶辫触锛?s' % error)

            if self.wait_for_wsd_port():
                break
            if attempt == 1:
                messages.append('3702 浠嶈鍗犵敤锛岄噸璇曚竴娆″仠姝㈡祦绋?)

        if not self.wait_for_wsd_port(0.0):
            try:
                holder = self.wsd_port_holder() or '鏈煡杩涚▼'
            except Error:
                holder = '鏈煡杩涚▼'
            messages.append('3702/udp 浠嶈鍗犵敤锛?s锛夛紝'
                            '鍥炲簲鍣ㄥ彲鑳借捣涓嶆潵锛涘彲鍦?NAS 涓婃墽琛?'
                            '`ss -lunp | grep 3702` 鎺掓煡' % holder)
        self.discovery['managed'] = True
        self.discovery['dropin'] = WSDD_DROPIN.exists()
        self.log('锛?.join(messages))
        return '锛?.join(messages)

    # ---- 3702 绔彛 -------------------------------------------------------
    def wsd_port_free(self) -> bool:
        """鑷繁璇曠潃缁戜竴娆?3702/udp锛氱粦寰椾笂璇存槑瀹樻柟鏈嶅姟纭疄鏀惧紑浜嗙鍙ｃ€?
        缁戝畾鍚庣珛鍒诲叧闂紙骞惰 SO_REUSEADDR锛夛紝涓嶅奖鍝嶉殢鍚?WsdResponder 鐨勭粦瀹氥€?        """
        import socket as _socket
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            probe.bind(('0.0.0.0', wsd.WSD_PORT))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def wsd_port_holder(self) -> str:
        """3702 琚皝鍗犵潃锛氫紭鍏?`ss -lunp`锛岄€€鑰屾眰鍏舵 `netstat -lunp`銆?""
        for command in (['ss', '-lunp'], ['netstat', '-lunp']):
            try:
                result = self.exec(command, timeout=10)
            except Error:
                continue
            for line in (result.stdout or '').splitlines():
                if ':%d' % wsd.WSD_PORT in line:
                    return line.strip()
        return ''

    def wait_for_wsd_port(self, seconds: float = None) -> bool:
        """绛夊畼鏂?wsdd 鐪熸閫€鍑猴紙鏈€澶?WSD_PORT_WAIT 绉掞級銆?""
        deadline = time.monotonic() + (WSD_PORT_WAIT if seconds is None else float(seconds))
        while True:
            try:
                if self.wsd_port_free():
                    return True
            except Exception as error:                          # noqa: BLE001 鍙槸鑷
                self.log('妫€鏌?3702 绔彛澶辫触锛?s' % error)
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def wsdd_restore(self) -> str:
        """鎶婂畼鏂?wsdd 鏀惧洖鍘伙細鍒犳帀鎴戜滑鐨?drop-in 鈫?daemon-reload 鈫?start wsdd銆?
        骞傜瓑锛歞rop-in 涓嶅湪灏辩洿鎺ヨ烦杩囷紱unit 涓嶅瓨鍦ㄤ篃鍙槸璁颁竴鏉℃棩蹇楋紝涓嶆姏寮傚父
        锛堣繖鏉¤矾寰勪細琚?`server.py --restore-wsdd` 鍦ㄦ湇鍔″仠鏈哄悗璋冪敤锛夈€?        """
        messages = []
        # 鍏堢‘淇?3702 宸茬粡鐢辨垜浠斁寮€锛堥噸澶嶈皟鐢ㄦ椂 responder 閫氬父宸茬粡鏄?None锛?        self.stop_responder()
        try:
            WSDD_DROPIN.unlink()
            messages.append('宸插垹闄?drop-in锛?s' % WSDD_DROPIN)
        except FileNotFoundError:
            messages.append('娌℃湁闇€瑕佹竻鐞嗙殑 drop-in')
        except OSError as error:
            messages.append('鍒犻櫎 drop-in 澶辫触锛?s' % error)

        for command, label in (
            (self.systemctl_command('daemon-reload'), 'daemon-reload'),
            (self.systemctl_command('start', WSDD_UNIT), '鍚姩瀹樻柟 wsdd'),
        ):
            try:
                result = self.exec(list(command), timeout=30)
                if result.ok:
                    messages.append(label + '鎴愬姛')
                else:
                    messages.append('%s杩斿洖闈?0锛坋xit %d锛夛細%s'
                                    % (label, result.returncode,
                                       (result.stderr or result.stdout).strip()))
            except Error as error:
                messages.append('%s澶辫触锛?s' % (label, error))
        self.discovery['managed'] = False
        self.discovery['dropin'] = WSDD_DROPIN.exists()
        self.log('锛?.join(messages))
        return '锛?.join(messages)

    # ---- 鍙戠幇鏈嶅姟鐢熷懡鍛ㄦ湡 -----------------------------------------------
    def new_responder(self):
        settings = self.settings
        return self.responder_factory(
            hostname=self.hostname,
            workgroup=self.workgroup,
            port=int(settings['metadataPort']),
            state_file=str(self.identity_file),
            hello_interval=int(settings['helloInterval']),
            on_log=self.on_responder_log,
        )

    def on_responder_log(self, message: str) -> None:
        text = str(message or '')
        if text.startswith('wsd http: POST'):
            action = ''
            if ' action=' in text:
                action = text.rsplit(' action=', 1)[1].strip()
            if action in ('', 'Get'):
                # 鍙湁 WS-Transfer Get 鎵嶆槸 Windows 鐪熺殑鏉ュ彇璁惧鎻忚堪
                self.discovery['metadataPosts'] = int(self.discovery['metadataPosts']) + 1
                self.discovery['lastMetadataAt'] = int(time.time())
        self.log('wsd: ' + text)

    def start(self) -> None:
        """鎸夎惤鐩樼殑寮€鍏冲惎鍔ㄥ彂鐜版湇鍔°€?
        - 寮€鍏虫墦寮€锛堥粯璁わ級锛氬厛鎺ョ瀹樻柟 wsdd锛屽啀璧锋湰杩涚▼鍐呯殑鍥炲簲鍣紱
        - 寮€鍏冲叧闂細鍙妸瀹樻柟 wsdd 鏀惧洖鍘伙紙骞傜瓑锛夛紝涓嶈捣鍥炲簲鍣紝
          `discovery['running']` 淇濇寔 False銆?        """
        with self.lock:
            self.hostname, self.workgroup = samba_identity()
            self.hostname = self._resolve_hostname()
            # 蹇呴』鍏堝仠鎺変笂涓€涓洖搴斿櫒鍐嶄涪寮冨紩鐢細
            # 瀹冩寔鏈?3702/5357 涓や釜 socket锛岀洿鎺?`self.responder = None` 浼氭紡鎺夊畠锛?            # 鏂板疄渚嬪啀缁?3702 灏卞繀鐒?`[Errno 98] Address already in use`锛?            # 椤甸潰浜庢槸鏄剧ず銆學SD 鍥炲簲鍣ㄥ惎鍔ㄥけ璐ャ€嶏紝鑰屽疄闄呬笂绔彛琚嚜宸辩殑鍍靛案瀹炰緥鍗犵潃
            # 锛堢湡鏈哄疄娴嬶細metadataPosts 浠嶅湪澧為暱锛岃鏄庤€佸疄渚嬭繕鍦ㄥ搷搴?Windows锛夈€?            self.stop_responder()
            self.responder = None
            enabled = bool(self.settings.get('discoveryEnabled', True))
            self.discovery['enabled'] = enabled
            if not enabled:
                self.wsdd_restore()
                self.discovery['running'] = False
                self.discovery['enabled'] = False
                self.discovery['msg'] = '缃戠粶鍙戠幇宸插叧闂紙瀹樻柟 wsdd 宸茶繕鍘燂紝鎻掍欢鏈帴绠★級'
                self.log(self.discovery['msg'])
                return
            self.wsdd_takeover()
            self._start_responder_locked()

    def _start_responder_locked(self) -> bool:
        """璧峰洖搴斿櫒骞舵妸鐘舵€佸啓杩?`self.discovery`锛涜繑鍥炴槸鍚︽垚鍔熴€?
        鍥炲簲鍣ㄥ惎鍔ㄨ閲嶈瘯鍑犳锛氱湡鏈轰笂銆屽仠瀹樻柟 wsdd銆嶄笌銆岃嚜宸辩粦 3702銆嶄箣闂村瓨鍦ㄧ珵鎬?        锛堝畼鏂硅繘绋嬪彲鑳借繕鍦ㄩ€€鍑轰腑锛夛紝涓€娆″け璐ュ氨姘镐箙鏀惧純浼氳椤甸潰涓€鐩存樉绀恒€屾湭杩愯銆嶃€?        """
        last_error = None
        for attempt in range(1, max(1, RESPONDER_START_RETRIES) + 1):
            responder = self.new_responder()
            try:
                responder.start()
            except Exception as error:                          # noqa: BLE001 绔彛琚崰涔熶笉鑳芥嫋鍨彃浠堕〉
                last_error = error
                try:
                    responder.stop()                            # 澶辫触瀹炰緥鑷繁鏀舵嬀锛堣 wsd.py锛?                except Exception:                               # noqa: BLE001
                    pass
                if attempt < max(1, RESPONDER_START_RETRIES):
                    self.log('WSD 鍥炲簲鍣ㄧ %d 娆″惎鍔ㄥけ璐ワ細%s锛?.1f 绉掑悗閲嶈瘯'
                             % (attempt, error, RESPONDER_START_GAP))
                    time.sleep(max(0.0, RESPONDER_START_GAP))
                    continue
                break
            self.responder = responder
            self.discovery['running'] = True
            self.discovery['startedAt'] = int(time.time())
            self.discovery['port'] = int(self.settings['metadataPort'])
            self.discovery['msg'] = '鍙戠幇鏈嶅姟杩愯涓?
            self.log('WSD 鍥炲簲鍣ㄥ凡鍚姩锛?s / %s锛?s锛?
                     % (self.hostname, self.workgroup, self.xaddrs_text()))
            return True
        self.discovery['running'] = False
        self.discovery['msg'] = 'WSD 鍥炲簲鍣ㄥ惎鍔ㄥけ璐ワ細%s' % last_error
        self.responder = None
        self.log(self.discovery['msg'])
        return False

    def xaddrs_text(self) -> str:
        try:
            return self.responder.xaddrs if self.responder else ''
        except Exception:                                       # noqa: BLE001
            return ''

    def stop_responder(self) -> None:
        responder, self.responder = self.responder, None
        if responder is None:
            return
        try:
            responder.stop()                                    # 鍙?Bye锛學indows 閲岀殑鍥炬爣浼氭秷澶?        except Exception as error:                              # noqa: BLE001
            self.log('鍋滄 WSD 鍥炲簲鍣ㄥ嚭閿欙細%s' % error)
        self.discovery['running'] = False
        self.discovery['msg'] = '鍙戠幇鏈嶅姟宸插仠姝?

    def restart_responder(self, times: int = 2) -> dict:
        """椤甸潰涓婄殑銆岄噸鏂板鍛娿€嶏細閲嶅缓鍥炲簲鍣ㄥ苟閲嶅彂 Hello銆?""
        with self.lock:
            self.stop_responder()
            self.hostname, self.workgroup = samba_identity()
            self.hostname = self._resolve_hostname()
            responder = self.new_responder()
            try:
                responder.start()
                self.responder = responder
                responder.announce_hello(times=times)
            except Exception as error:                          # noqa: BLE001
                self.responder = None
                self.discovery['running'] = False
                self.discovery['msg'] = '閲嶆柊瀹ｅ憡澶辫触锛?s' % error
                self.log(self.discovery['msg'])
                raise Error(self.discovery['msg']) from error
            self.discovery['running'] = True
            self.discovery['startedAt'] = int(time.time())
            self.discovery['port'] = int(self.settings['metadataPort'])
            self.discovery['msg'] = '宸查噸鏂板鍛?
            self.log('宸查噸鏂板鍛婏紙Hello x%d锛? % max(1, times))
        return self.snapshot()

    # ---- 寮€鍏充笌涓绘満鍚嶏紙椤甸潰涓婄殑涓や釜鍐欐搷浣滐級------------------------------
    def set_discovery_enabled(self, enabled) -> dict:
        """鎵撳紑 = 鎺ョ瀹樻柟 wsdd + 璧峰洖搴斿櫒锛涘叧闂?= 鍙?Bye + 鎶婂畼鏂?wsdd 鍘熸牱杩樺師銆?
        鐘舵€佽惤鐩樺埌 `settings.json`锛屾墍浠ユ湇鍔￠噸鍚悗鎸夎繖閲岀殑缁撴灉鐢熸晥銆?        """
        target = normalize_discovery_enabled(enabled, bool(self.settings.get('discoveryEnabled')))
        with self.lock:
            self.settings['discoveryEnabled'] = bool(target)
            save_settings(self.settings, self.state_file)
            self.discovery['enabled'] = bool(target)
            if target:
                self.hostname, self.workgroup = samba_identity()
                self.hostname = self._resolve_hostname()
                self.wsdd_takeover()
                self._start_responder_locked()
                self.log('缃戠粶鍙戠幇宸叉墦寮€锛氬凡鎺ョ瀹樻柟 wsdd 骞跺惎鍔ㄥ洖搴斿櫒')
            else:
                # 骞傜瓑锛氬彂 Bye 鈫?鍒?drop-in 鈫?daemon-reload 鈫?start wsdd銆?                # 瀹樻柟 wsdd 鏈潵灏卞湪璺戙€乨rop-in 涓嶅瓨鍦ㄦ椂涔熻鎴愬姛杩斿洖銆?                self.wsdd_restore()
                self.discovery['running'] = False
                self.discovery['enabled'] = False
                self.discovery['managed'] = False
                self.discovery['msg'] = '缃戠粶鍙戠幇宸插叧闂紙瀹樻柟 wsdd 宸茶繕鍘燂紝鎻掍欢鏈帴绠★級'
                self.log(self.discovery['msg'])
        return self.snapshot()

    # ---- 韬唤灞曠ず锛堝彧璇伙級涓庛€屾仮澶嶄负绯荤粺涓绘満鍚嶃€?-------------------------
    def system_hostname(self) -> str:
        """绯荤粺涓绘満鍚嶏紙鑷姩鑾峰彇锛岃 `resolve_system_hostname()`锛夈€?""
        return resolve_system_hostname()

    def smb_netbios_name(self) -> str:
        """SMB / NetBIOS 鍚嶏細`/var/etc/smb.conf` 鐨?`netbios name` 鈫?samba 鐨?`option name`銆?""
        fallback = samba_config_value('name', DEFAULT_HOSTNAME)
        return smb_conf_netbios_name(self.read_config_soft('smb.conf'), fallback)

    def lan_address(self, force: bool = False) -> str:
        """NAS 鐨勫眬鍩熺綉鍦板潃锛堣嚜鍔ㄦ帰娴嬶紝缂撳瓨 30 绉掞級锛氬彇涓嶅埌杩斿洖绌轰覆銆?""
        now = time.time()
        stamp, cached = self.address_cache
        if not force and cached and now - stamp < 30:
            return cached
        value = probe_lan_address()
        self.address_cache = (now, value)
        return value

    def identity_snapshot(self, address: str = '') -> dict:
        """椤甸潰鍙灞曠ず鐨勮韩浠斤細SMB 鍚嶃€佸眬鍩熺綉鍦板潃涓庤闂彁绀猴紙+ 鍐呴儴鍒ゆ嵁锛夈€?
        鍚嶅瓧**鍏ㄩ儴鑷姩鑾峰彇**锛堜笉钀界洏銆佷篃涓嶆帴鍙楃敤鎴疯緭鍏ワ級锛?        `netbiosName` = smb.conf 鐨?`netbios name` 鈫?samba 鐨?`option name`锛?*椤甸潰鍙樉绀哄畠**锛夛紱
        `systemName` = uci/system 涓绘満鍚?鈫?samba 鍚嶏紝鍙敤鏉ュ垽鏂袱鑰呮槸鍚︿竴鑷达紙椤甸潰涓嶆樉绀猴級銆?        涓嶄竴鑷存椂缁?`warning`锛岄〉闈㈡嵁姝ゆ樉绀洪粍鑹叉彁绀轰笌銆屾仮澶嶄负绯荤粺涓绘満鍚嶃€嶆寜閽€?        `hint` 鐢?SMB 鍚?+ 灞€鍩熺綉 IP 鎷硷紙瑙?access_hint()锛夈€?        """
        system_name = self.system_hostname()
        netbios = self.smb_netbios_name()
        address = str(address or '').strip() or self.lan_address()
        matched = bool(system_name) and system_name.lower() == netbios.lower()
        warning = ''
        if not matched:
            warning = ('SMB 鍚嶄笌绯荤粺涓绘満鍚嶄笉涓€鑷达細SMB 鍚?%s 鈮?绯荤粺涓绘満鍚?%s銆俓n'
                       'Windows 11 闈犵郴缁熶富鏈哄悕锛圵SD/mDNS/DNS锛夊彂鐜拌澶囷紝涓嶄竴鑷存椂浼氳繛涓嶄笂锛?
                       '鐐逛笅闈㈢殑銆屾仮澶嶄负绯荤粺涓绘満鍚嶃€嶏紝鎻掍欢浼氭妸 Samba 鐨勫悕瀛楁敼鎴愮郴缁熶富鏈哄悕骞堕噸鍚湇鍔°€?
                       % (netbios or '锛堣涓嶅埌锛?, system_name or '锛堣涓嶅埌锛?))
        return {
            # 绯荤粺涓绘満鍚嶅彧浣滀负銆屼袱鍚嶆槸鍚︿竴鑷淬€嶇殑鍒ゆ嵁锛岄〉闈笉鍐嶅睍绀哄畠
            'systemName': system_name,
            'netbiosName': netbios,
            'matched': matched,
            'address': address,
            'hint': access_hint(netbios, address),
            'warning': warning,
        }

    def restore_smb_hostname(self) -> dict:
        """鎶?Samba 鐨?`option name`锛堚啋 `netbios name`锛夋仮澶嶆垚**绯荤粺涓绘満鍚?*銆?
        鍚嶅瓧鍙湁涓€涓潵婧愶細鑷姩鑾峰彇鐨勭郴缁熶富鏈哄悕锛?*涓嶆帴鍙?*鐢ㄦ埛鎻愪緵鐨勫悕瀛楋級銆?        姝ラ锛氬啓 `/etc/config/samba` 鐨?`option name` 鈫?`smb_mgr.sh init_config`
        鈫?**`systemctl restart smb nmb wsdd`**锛坄netbios name` 鍙?reload 涓嶇敓鏁堬紝蹇呴』
        restart锛夆啋 鍥炶 `smb.conf` 鐨?`netbios name` 鏍￠獙锛涜繑鍥?`verified` 涓庡け璐ュ師鍥狅紝
        鐢遍〉闈㈡彁绀虹粨鏋溿€傚彂鐜版湇鍔″湪璺戞椂椤哄甫閲嶅缓鍥炲簲鍣紙瀹ｅ憡鍚嶈窡鐫€閰嶇疆璧帮級銆?        """
        name = self.system_hostname()
        if not hostname_valid(name):
            raise Error('绯荤粺涓绘満鍚?%s 涓嶈兘浣滀负 SMB 鍚嶏紙1-15 浣嶏紝瀛楁瘝/鏁板瓧/鐐?涓嬪垝绾?鐭í绾匡級'
                        % (name or '锛堢┖锛?))
        commands = []
        with self.lock:
            path = self._config_path('samba')
            try:
                text = self.read_config('samba')
            except Error:
                text = ''
            updated = set_uci_option(text, 'samba', 'name', name)
            if updated != text:
                atomic_write(path, updated)
                self.log('鎶?/etc/config/samba 鐨?option name 鏀规垚绯荤粺涓绘満鍚?%s' % name)
            result = self.exec(init_config_command(self.samba_mgr_path()), timeout=60)
            commands.append(describe(init_config_command(self.samba_mgr_path()), result))
            restart_command = [self.systemctl, 'restart', *SMB_RESTART_UNITS]
            restart = self.exec(restart_command, timeout=90)
            commands.append(describe(restart_command, restart))
            # 鍥炶鏍￠獙锛歴mb.conf 閲岀殑 netbios name 蹇呴』灏辨槸绯荤粺涓绘満鍚?            netbios = self.smb_netbios_name()
            verified = netbios.lower() == name.lower()
            errors = []
            if not result.ok:
                errors.append(translate_failure(
                    '%s %s' % (result.stdout, result.stderr), '鐢熸垚 smb.conf'))
            if not restart.ok:
                errors.append('閲嶅惎鏈嶅姟杩斿洖闈?0锛坋xit %d锛夛紝`netbios name` 鍙兘杩樻病鐢熸晥'
                              % restart.returncode)
            if not verified:
                errors.append('smb.conf 閲岀殑 netbios name 浠嶆槸 %s锛堟湡鏈?%s锛?
                              % (netbios or '锛堢┖锛?, name))
            # 瀹ｅ憡鍚嶈窡鐫€閰嶇疆璧帮細閲嶅缓鍥炲簲鍣紙瀹樻柟 wsdd 宸茬粡鏄┖鎿嶄綔锛屼笉浼氫簰鐩告姠 3702锛?            if self.responder is not None and bool(self.discovery['running']):
                self.hostname, self.workgroup = samba_identity()
                self.hostname = self._resolve_hostname()
                self.stop_responder()
                self._start_responder_locked()
            self.discovery['msg'] = ('宸叉仮澶嶄负绯荤粺涓绘満鍚?%s' % name if verified
                                     else '鎭㈠涓虹郴缁熶富鏈哄悕鍚庢牎楠屾湭閫氳繃')
        self.log('鎭㈠涓虹郴缁熶富鏈哄悕 %s锛坣etbios 鍥炶 %s锛宺estart %s锛?
                 % (name, netbios or '锛堢┖锛?, '鎴愬姛' if restart.ok else '闈?0'))
        return {
            'name': name,
            'netbiosName': netbios,
            'verified': bool(verified),
            'matched': bool(verified),
            'initReturncode': result.returncode,
            'restartReturncode': restart.returncode,
            'units': list(SMB_RESTART_UNITS),
            'errors': errors,
            'output': '\n'.join(commands),
        }

    def shutdown(self) -> None:
        """鏈嶅姟鍋滄锛氬厛鍙?Bye锛屽啀鎶婂畼鏂?wsdd 鏀惧洖鍘汇€?""
        self.stop_responder()
        self.wsdd_restore()

    # ---- 鐘舵€佸揩鐓?-------------------------------------------------------
    def settings_snapshot(self) -> dict:
        """椤甸潰瑕佺敤鐨勮惤鐩樿缃細鍙墿銆岀綉缁滃彂鐜般€嶅紑鍏筹紙涓绘満鍚嶅凡鏀逛负鍙灞曠ず锛夈€?""
        return {
            'discoveryEnabled': bool(self.settings.get('discoveryEnabled', True)),
        }

    def status(self) -> dict:
        responder = self.responder
        xaddrs = []
        address = ''
        identity = ''
        metadata_port = int(self.settings['metadataPort'])
        if responder is not None:
            try:
                xaddrs = [responder.xaddrs]
                address = getattr(responder, 'address', '') or ''
                identity = getattr(responder, 'identity', '') or ''
                metadata_port = int(getattr(responder, 'port', metadata_port) or metadata_port)
            except Exception:                                   # noqa: BLE001
                xaddrs = []
        if not xaddrs and address:
            xaddrs = lan_xaddrs(address, metadata_port)
        enabled = bool(self.settings.get('discoveryEnabled', True))
        running = bool(self.discovery['running'])
        discovery = {
            'running': running,
            'enabled': enabled,
            'startedAt': int(self.discovery['startedAt']),
            'message': self.discovery['msg'] or (
                '鍙戠幇鏈嶅姟杩愯涓? if running else (
                    '缃戠粶鍙戠幇宸插叧闂? if not enabled else '鍙戠幇鏈嶅姟鏈繍琛?)),
            'hostname': self.hostname,
            'workgroup': self.workgroup,
            'xaddrs': xaddrs,
            'address': address,
            'identity': identity,
            'metadataPort': metadata_port,
            'managed': bool(self.discovery['managed']),
            'wsddDropin': bool(self.discovery['dropin']),
            'wsddUnit': WSDD_UNIT,
            'wsddDropinPath': str(WSDD_DROPIN),
            'metadataPosts': int(self.discovery['metadataPosts']),
            'lastMetadataAt': int(self.discovery['lastMetadataAt']),
            'helloInterval': int(self.settings['helloInterval']),
        }
        return {
            'ok': True,
            'version': installed_version(),
            # 椤甸潰鍙 settings锛堝彧鍓┿€岀綉缁滃彂鐜般€嶅紑鍏筹級锛涗富鏈哄悕/SMB 鍚嶈蛋 identity锛堣嚜鍔ㄨ幏鍙栵級锛?            # discovery 閲岀殑宸ヤ綔缁?鍦板潃/瀹樻柟 wsdd 鐘舵€佺瓑瀛楁浠嶇劧杩斿洖锛屾柟渚跨湡鏈烘帓鏌ャ€?            'settings': self.settings_snapshot(),
            'identity': self.identity_snapshot(address),
            'discovery': discovery,
            'accounts': self.accounts(),
            'allowedRoots': self.allowed_roots(),
            'sambaMgr': self.samba_mgr_path(),
            'sambaMgrFound': os.path.exists(self.samba_mgr_path()),
            'smbUnits': list(SMB_UNITS),
        }

    def snapshot(self) -> dict:
        return self.status()


# ---------------------------------------------------------------------------
# 渚?CLI / 娴嬭瘯浣跨敤鐨勮交閲忓叆鍙?# ---------------------------------------------------------------------------

def restore_wsdd(data_dir=None, runner=None) -> str:
    """鍙仛銆屾妸瀹樻柟 wsdd 鏀惧洖鍘汇€嶈繖涓€浠朵簨锛屼笉鍚姩鍥炲簲鍣ㄣ€佷笉璧?HTTP 鏈嶅姟銆?""
    engine = Engine(data_dir=data_dir, runner=runner, start_responder=False)
    return engine.wsdd_restore()
