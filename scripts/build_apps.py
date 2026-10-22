#!/usr/bin/env python3
"""Package plugin projects into apps/ and generate apps.json.

Usage:
  python3 scripts/build_apps.py
  python3 scripts/build_apps.py --signing-key ~/.local/share/xiaomi-community-store/signing-key.pem

Output (committed to git):
  apps/<id>-<version>.zip
  apps/icons/<id>.png
  apps.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "projects"
APPS = ROOT / "apps"
APPS_JSON = ROOT / "apps.json"

# ---------------------------------------------------------------------------
# Package specs 鈥?one entry per publishable plugin.
# Keys match the existing build_repository.py so bundles stay compatible.
# ---------------------------------------------------------------------------
PACKAGE_SPECS: list[dict[str, Any]] = [
    {
        "id": "devicemanager",
        "name": "璁惧绠″",
        "version": "0.4.1",
        "summary": "2 绉掑疄鏃剁洃鎺х郴缁熴€佺綉缁溿€佺鐩樺仴搴蜂笌 Docker 瀹瑰櫒璧勬簮",
        "description": "鍙鏌ョ湅 CPU銆佸唴瀛樸€佹俯搴︺€佺綉缁溿€佺鐩?SMART 涓?Docker 瀹瑰櫒璧勬簮鍗犵敤锛岄〉闈㈠彲瑙佹椂姣?2 绉掑埛鏂般€?,
        "project": "xiaomi-device-manager-prototype",
        "pluginId": 11001,
        "port": 18080,
        "releaseRoot": "/data/plugin/xiaomi-device-manager",
        "uiKey": "devicemanager",
        "iconSource": "assets/xiaomi-device-manager-v2.png",
        "iconName": "xiaomi-device-manager-v2.icon",
        "runtime": {
            "scripts/nas_status_server.py": "server.py",
            "dist/client": "public",
        },
        "ui": "dist/client",
        "serviceSource": "deploy/xiaomi-device-manager.service",
        "service": "xiaomi-device-manager.service",
        "nginxSource": "deploy/xiaomi-device-manager.nginx.conf",
        "nginx": "xiaomi-device-manager.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "monitoring"],
        "author": "Kingwell",
        "registry": {
            "icon": "/icon/xiaomi-device-manager-v2.icon?v=2",
            "frontend": {
                "title": "璁惧绠″",
                "desc": "璁惧鐘舵€佺瀹?,
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/deviceManager_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/deviceManager_pc"},
                ],
                "sortid": 11001,
                "widget": [],
            },
            "info": {
                "tags": ["tool", "monitoring"],
                "desc": "鐩戞帶 NAS 绯荤粺銆佺綉缁溿€佺‖鐩樺仴搴蜂笌 Docker 瀹瑰櫒璧勬簮",
                "developer": "Kingwell Community",
                "publisher": "community",
                "ext": {"admin": True},
            },
        },
    },
    {
        "id": "115sync",
        "name": "115 浜戝浠?,
        "version": "0.1.0",
        "summary": "浣跨敤 115 瀹樻柟 OpenAPI 鍚屾涓庡浠?NAS 鏂囦欢",
        "description": "闇€瑕佽嚜宸辫幏鎵圭殑 115 App ID锛屾壂鐮佹巿鏉冨悗鍚屾涓庡浠?NAS 鏂囦欢銆?,
        "project": "xiaomi-115-sync-plugin",
        "pluginId": 1000,
        "port": 18115,
        "releaseRoot": "/data/plugin/115-sync",
        "uiKey": "115sync",
        "iconSource": "web/assets/115-sync-icon.png",
        "iconName": "115-sync.icon",
        "runtime": {"server.py": "server.py", "requirements.txt": "requirements.txt"},
        "requirements": "runtime/requirements.txt",
        "ui": "web",
        "serviceSource": "deploy/xiaomi-115-sync.service",
        "service": "xiaomi-115-sync.service",
        "nginxSource": "deploy/xiaomi-115-sync.nginx.conf",
        "nginx": "xiaomi-115-sync.conf",
        "healthPath": "/api/status",
        "tags": ["cloud", "backup"],
        "author": "community",
        "registry": {
            "icon": "/icon/115-sync.icon?v=115life-38.2.0",
            "frontend": {
                "title": "115 浜戝浠?,
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/115Sync_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/115Sync_pc"},
                ],
                "sortid": 1000,
                "widget": [],
            },
            "info": {"tags": ["cloud", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "aliyundrivesync",
        "name": "闃块噷浜戠洏澶囦唤",
        "version": "0.1.0",
        "summary": "鎵爜杩炴帴闃块噷浜戠洏骞跺悓姝?NAS 鏂囦欢",
        "description": "闇€瑕佽嚜宸辫幏鎵圭殑闃块噷浜戠洏搴旂敤锛屾壂鐮佹巿鏉冨悗鍚屾 NAS 鏂囦欢銆?,
        "project": "xiaomi-aliyundrive-sync-plugin",
        "pluginId": 1002,
        "port": 18117,
        "releaseRoot": "/data/plugin/aliyundrive-sync",
        "uiKey": "aliyundrivesync",
        "iconSource": "web/assets/aliyundrive-icon.png",
        "iconName": "aliyundrive-sync.icon",
        "runtime": {"server.py": "server.py"},
        "ui": "web",
        "serviceSource": "deploy/xiaomi-aliyundrive-sync.service",
        "service": "xiaomi-aliyundrive-sync.service",
        "nginxSource": "deploy/xiaomi-aliyundrive-sync.nginx.conf",
        "nginx": "xiaomi-aliyundrive-sync.conf",
        "healthPath": "/api/status",
        "tags": ["cloud", "backup"],
        "author": "community",
        "registry": {
            "icon": "/icon/aliyundrive-sync.icon?v=0.1.0",
            "frontend": {
                "title": "闃块噷浜戠洏澶囦唤",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/aliyunDriveSync_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/aliyunDriveSync_pc"},
                ],
                "sortid": 1002,
                "widget": [],
            },
            "info": {"tags": ["cloud", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "webdav",
        "name": "WebDAV 鏂囦欢妗?,
        "version": "0.2.0-rc5",
        "summary": "NAS HTTPS 鏂囦欢鍏变韩锛屼互鍙婅繙绋?WebDAV 涓婁紶銆佷笅杞戒笌瀹氭椂澶囦唤",
        "description": "澶氳处鍙枫€佸鐩綍鎺堟潈銆佽繙绋嬪崟鍚戝浠姐€佺洰褰曟柊寤轰笌鍘熶綅鏀瑰悕銆傛祴璇曠増銆?,
        "project": "xiaomi-webdav-plugin",
        "pluginId": 11003,
        "port": 18120,
        "releaseRoot": "/data/plugin/webdav",
        "uiKey": "webdav",
        "iconSource": "web/assets/webdav.png",
        "iconName": "webdav-file-bridge.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "access.py": "access.py",
            "multi_dav.py": "multi_dav.py",
            "compatibility.py": "compatibility.py",
            "vendor": "vendor",
            "vendor-integrity.json": "vendor-integrity.json",
            "scripts/check_after_upgrade.py": "scripts/check_after_upgrade.py",
            "requirements-dav.txt": "requirements-dav.txt",
            "bin": "bin",
            "web": "web",
            "licenses": "licenses",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-webdav.service",
        "service": "xiaomi-webdav.service",
        "nginxSource": "deploy/xiaomi-webdav.nginx.conf",
        "nginx": "xiaomi-webdav.conf",
        "healthPath": "/healthz",
        "tags": ["files", "backup"],
        "author": "community",
        "registry": {
            "icon": "/icon/webdav-file-bridge.icon?v=0.1.0",
            "frontend": {
                "title": "WebDAV 鏂囦欢妗?,
                "desc": "鏂囦欢鍏变韩涓庡浠?,
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/webDavBridge_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/webDavBridge_pc"},
                ],
                "sortid": 11003,
                "widget": [],
            },
            "info": {"tags": ["files", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "qbittorrent",
        "name": "qB 涓嬭浇",
        "version": "0.1.2",
        "summary": "纾佸姏涓庣瀛愪笅杞姐€佹殏鍋滅户缁拰闄愰€燂紱棣栨鍚姩闇€鎷夊彇鐙珛 Docker 闀滃儚",
        "description": "纾佸姏/绉嶅瓙涓嬭浇銆佽繘搴︺€佹殏鍋滅户缁€侀檺閫熴€傞渶瑕?Docker銆?,
        "project": "xiaomi-qbittorrent-plugin",
        "pluginId": 11004,
        "port": 18122,
        "releaseRoot": "/data/plugin/qbittorrent",
        "uiKey": "qbittorrent",
        "iconSource": "web/assets/qb.png",
        "iconName": "qbittorrent.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "upnp.py": "upnp.py",
            "web": "web",
            "licenses": "licenses",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-qbittorrent.service",
        "service": "xiaomi-qbittorrent.service",
        "nginxSource": "deploy/xiaomi-qbittorrent.nginx.conf",
        "nginx": "xiaomi-qbittorrent.conf",
        "healthPath": "/healthz",
        "tags": ["download"],
        "author": "community",
        "registry": {
            "icon": "/icon/qbittorrent.icon?v=0.1.2",
            "frontend": {
                "title": "qB 涓嬭浇",
                "desc": "涓嬭浇浠诲姟绠＄悊",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/qbDownloads_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/qbDownloads_pc"},
                ],
                "sortid": 11004,
                "widget": [],
            },
            "info": {"tags": ["download"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "sshcontrol",
        "name": "SSH 寮€鍏?,
        "version": "0.1.0",
        "summary": "鍚仠 SSH 杩滅▼鐧诲綍锛屽苟璁剧疆寮€鏈鸿嚜鍔ㄤ繚鎸佽繍琛?,
        "description": "鍦ㄥ鎴风鍐呭惎鍋?SSH锛屾敮鎸佸紑鏈鸿嚜鍚€傜敤浜庣粫杩?boot_check.sh 姣忔寮€鏈哄己鍒跺叧闂?SSH 鐨勮涓恒€?,
        "project": "xiaomi-ssh-control-plugin",
        "pluginId": 11005,
        "port": 18130,
        "releaseRoot": "/data/plugin/ssh-control",
        "uiKey": "sshcontrol",
        "iconSource": "web/assets/ssh-control-icon.png",
        "iconName": "ssh-control.icon",
        "runtime": {
            "server.py": "server.py",
            "keepalive.sh": "keepalive.sh",
            "hotplug.sh": "hotplug.sh",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-ssh-control.service",
        "service": "xiaomi-ssh-control.service",
        "nginxSource": "deploy/xiaomi-ssh-control.nginx.conf",
        "nginx": "xiaomi-ssh-control.conf",
        "healthPath": "/api/status",
        "tags": ["tool", "system"],
        "author": "community",
        "registry": {
            "icon": "/icon/ssh-control.icon?v=1",
            "frontend": {
                "title": "SSH 寮€鍏?,
                "desc": "杩滅▼鐧诲綍鎺у埗",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/sshControl_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/sshControl_pc"},
                ],
                "sortid": 11005,
                "widget": [],
            },
            "info": {"tags": ["tool", "system"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "transmission",
        "name": "Transmission 涓嬭浇",
        "version": "0.2.0",
        "summary": "Docker 鐗?BT 涓嬭浇锛氬彲璁剧疆涓嬭浇/閰嶇疆/鐩戞帶鐩綍涓?WebUI 璐﹀彿锛涚鍙?9091銆?1413",
        "description": "Docker 瀹瑰櫒杩愯 LinuxServer Transmission锛涢厤缃〉鍙缃笅杞界洰褰曘€侀厤缃枃浠跺す鐩綍銆佺洃鎺х洰褰曚笌 WebUI 鐢ㄦ埛鍚嶅瘑鐮併€傞渶瑕?Docker銆?,
        "project": "xiaomi-transmission-plugin",
        "pluginId": 11006,
        "port": 18140,
        "releaseRoot": "/data/plugin/transmission",
        "uiKey": "transmission",
        "iconSource": "web/assets/transmission.png",
        "iconName": "transmission.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "upnp.py": "upnp.py",
            "web": "web",
            "licenses": "licenses",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-transmission.service",
        "service": "xiaomi-transmission.service",
        "nginxSource": "deploy/xiaomi-transmission.nginx.conf",
        "nginx": "xiaomi-transmission.conf",
        "healthPath": "/healthz",
        "tags": ["download"],
        "author": "community",
        "registry": {
            "icon": "/icon/transmission.icon?v=0.2.0",
            "frontend": {
                "title": "Transmission 涓嬭浇",
                "desc": "涓嬭浇閰嶇疆绠＄悊",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/transmissionDownload_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/transmissionDownload_pc"},
                ],
                "sortid": 11006,
                "widget": [],
            },
            "info": {"tags": ["download"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "emby",
        "name": "Emby",
        "version": "0.1.0",
        "summary": "鍦?NAS 涓婅繍琛?Emby锛屽悜灞€鍩熺綉璁惧涓叉祦涓汉褰遍煶",
        "description": "瀹樻柟 Emby 闀滃儚鐨勫彈闄愬鍣紱濯掍綋鐩綍璇诲啓鎸傝浇浠ヤ究鍐欏洖鍏冩暟鎹紝閰嶇疆淇濆瓨鍦ㄦ彃浠剁鏈夌洰褰曪紝Emby 榛樿绔彛 8096 瀵瑰眬鍩熺綉寮€鏀俱€?,
        "project": "xiaomi-emby-plugin",
        "pluginId": 11007,
        "port": 18150,
        "releaseRoot": "/data/plugin/emby",
        "uiKey": "emby",
        "iconSource": "web/assets/emby.png",
        "iconName": "emby.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-emby.service",
        "service": "xiaomi-emby.service",
        "nginxSource": "deploy/xiaomi-emby.nginx.conf",
        "nginx": "xiaomi-emby.conf",
        "healthPath": "/healthz",
        "tags": ["media"],
        "author": "community",
        "registry": {
            "icon": "/icon/emby.icon?v=0.1.0",
            "frontend": {
                "title": "Emby",
                "desc": "褰遍煶涓叉祦",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/embyMedia_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/embyMedia_pc"},
                ],
                "sortid": 11007,
                "widget": [],
            },
            "info": {"tags": ["media"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "dpanel",
        "name": "DPanel 瀹瑰櫒绠＄悊",
        "version": "0.1.0",
        "summary": "杞婚噺 Docker 绠＄悊闈㈡澘锛涘彲瑙嗗寲绠＄悊瀹瑰櫒銆侀暅鍍忎笌 Compose锛涢渶瑕?Docker",
        "description": "DPanel Lite 闀滃儚鐨勫彈闄愬鍣紱鎸傝浇 Docker socket 涓庡彲閫夐厤缃洰褰曪紝闈㈡澘绔彛 8807 瀵瑰眬鍩熺綉寮€鏀俱€?,
        "project": "xiaomi-dpanel-plugin",
        "pluginId": 11008,
        # 18110锛氬師鏉ヨ繖閲屽啓鐨?18160 涓庛€岀‖鐩樹紤鐪犮€嶆挒鍙蜂簡锛坉isksleep 鎵嶆槸鍦?18160
        # 涓婄洃鍚殑锛孨AS 涓婂疄娴嬬‘璁わ級銆傛竻鍗曢噷鐨勭鍙ｄ細涓嬪彂缁欐彃浠跺寘涓庡晢搴楋紝鍐欓敊浼氳
        # 涓や釜鎻掍欢鎶㈠悓涓€涓鍙ｏ紝鎵€浠ヨ繖閲屾敼鎴愭湭鍗犵敤鐨?18110銆?
        "port": 18110,
        "releaseRoot": "/data/plugin/dpanel",
        "uiKey": "dpanel",
        "iconSource": "web/assets/dpanel.png",
        "iconName": "dpanel.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-dpanel.service",
        "service": "xiaomi-dpanel.service",
        "nginxSource": "deploy/xiaomi-dpanel.nginx.conf",
        "nginx": "xiaomi-dpanel.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "docker"],
        "author": "community",
        "registry": {
            "icon": "/icon/dpanel.icon?v=0.1.0",
            "frontend": {
                "title": "DPanel 瀹瑰櫒绠＄悊",
                "desc": "Docker 闈㈡澘",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/dpanelManage_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/dpanelManage_pc"},
                ],
                "sortid": 11008,
                "widget": [],
            },
            "info": {"tags": ["tool", "docker"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "jellyfin",
        "name": "Jellyfin",
        "version": "0.1.0",
        "summary": "寮€婧愬獟浣撴湇鍔″櫒锛涢厤缃?缂撳瓨/濯掍綋鎸佷箙鍖栵紝HTTP 绔彛 8097 瀵瑰眬鍩熺綉寮€鏀?,
        "description": "瀹樻柟 Jellyfin 闀滃儚鐨勫彈闄愬鍣紱濯掍綋鐩綍銆侀厤缃洰褰曚笌缂撳瓨鎸佷箙鍖栵紝绔彛鏄犲皠 8097/8920/7359锛屽彲涓?Emby 鍚屾椂瀹夎銆傞渶瑕?Docker銆?,
        "project": "xiaomi-jellyfin-plugin",
        "pluginId": 11009,
        "port": 18170,
        "releaseRoot": "/data/plugin/jellyfin",
        "uiKey": "jellyfin",
        "iconSource": "web/assets/jellyfin.png",
        "iconName": "jellyfin.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-jellyfin.service",
        "service": "xiaomi-jellyfin.service",
        "nginxSource": "deploy/xiaomi-jellyfin.nginx.conf",
        "nginx": "xiaomi-jellyfin.conf",
        "healthPath": "/healthz",
        "tags": ["media"],
        "author": "community",
        "registry": {
            "icon": "/icon/jellyfin.icon?v=0.1.0",
            "frontend": {
                "title": "Jellyfin",
                "desc": "褰遍煶涓叉祦",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/jellyfinMedia_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/jellyfinMedia_pc"},
                ],
                "sortid": 11009,
                "widget": [],
            },
            "info": {"tags": ["media"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "fwclient",
        "name": "鍐呯綉绌块€?,
        "version": "0.1.0",
        "summary": "fwclient 鍐呯綉绌块€忓鎴风锛涘彲閰嶆湇鍔″櫒鍩熷悕涓庝护鐗岋紝鏄剧ず鐗堟湰骞舵敮鎸佸崌绾?,
        "description": "闅忓寘鎼哄甫 fwclient-linux-arm64銆備紭鍏堜互瀹堟姢杩涚▼杩愯 fwclient -s <鍩熷悕> -t <浠ょ墝> -d锛沀I 鏄剧ず褰撳墠鐗堟湰锛屾敮鎸?-u 鍗囩骇銆?,
        "project": "xiaomi-fwclient-plugin",
        "pluginId": 11010,
        "port": 18180,
        "releaseRoot": "/data/plugin/fwclient",
        "uiKey": "fwclient",
        "iconSource": "web/assets/fwclient.png",
        "iconName": "fwclient.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "bin": "bin",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-fwclient.service",
        "service": "xiaomi-fwclient.service",
        "nginxSource": "deploy/xiaomi-fwclient.nginx.conf",
        "nginx": "xiaomi-fwclient.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "network"],
        "author": "community",
        "registry": {
            "icon": "/icon/fwclient.icon?v=0.1.0",
            "frontend": {
                "title": "鍐呯綉绌块€?,
                "desc": "fwclient 闅ч亾",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/fwTunnel_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/fwTunnel_pc"},
                ],
                "sortid": 11010,
                "widget": [],
            },
            "info": {"tags": ["tool", "network"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "disksleep",
        "name": "纭洏浼戠湢",
        "version": "0.1.0",
        "summary": "涓庣郴缁熶紤鐪犲紑鍏宠仈鍔紝鍙嚜瀹氫箟浼戠湢鏃堕棿骞舵煡鐪嬩紤鐪?鍞ら啋鏃ュ織",
        "description": "绯荤粺璁剧疆鐨勭‖鐩樹紤鐪犲彧鏈夊紑鍏充笖鍥哄畾 30 鍒嗛挓銆傛湰鎻掍欢娌跨敤鍚屼竴涓紑鍏筹紝"
                       "鐢?systemd drop-in 瑕嗙洊 hdidle 鐨勬椂闀匡紝骞舵彁渚涗紤鐪?鍞ら啋浜嬩欢涓?hdidle 鍘熷鏃ュ織銆?,
        "project": "xiaomi-disk-sleep-plugin",
        "pluginId": 11011,
        "port": 18160,
        "releaseRoot": "/data/plugin/disk-sleep",
        "uiKey": "disksleep",
        "iconSource": "web/assets/disk-sleep-icon.png",
        "iconName": "disk-sleep.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-disk-sleep.service",
        "service": "xiaomi-disk-sleep.service",
        "nginxSource": "deploy/xiaomi-disk-sleep.nginx.conf",
        "nginx": "xiaomi-disk-sleep.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "system"],
        "author": "community",
        "registry": {
            "icon": "/icon/disk-sleep.icon?v=0.1.0",
            "frontend": {
                "title": "纭洏浼戠湢",
                "desc": "浼戠湢鏃堕棿涓庢棩蹇?,
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/diskSleep_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/diskSleep_pc"},
                ],
                "sortid": 11011,
                "widget": [],
            },
            "info": {"tags": ["tool", "system"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "netneighbor",
        "name": "缃戠粶閭诲眳",
        "version": "0.1.1",
        "summary": "璁╁皬绫冲瓨鍌ㄥ湪 Windows銆岀綉缁溿€嶉噷鐩存帴鍙锛屽苟鏀寔涓€涓处鍙锋寕澶氫釜鍏变韩鐩綍",
        "description": "Windows 鐨勮祫婧愮鐞嗗櫒銆岀綉缁溿€嶉噷涓€鐩寸湅涓嶅埌灏忕背瀛樺偍锛屽彧鑳芥墜杈?IP 璁块棶銆傛湰鎻掍欢鑷甫 WSD "
                       "鍥炲簲鍣紙Hello/Bye/Probe/Resolve 涓庤澶囨弿杩版湇鍔★級鎺ョ绯荤粺閭ｅ涓嶅畬鏁寸殑鍙戠幇鏈嶅姟锛屼富鏈哄嵆鍙嚭鐜板湪"
                       "缃戠粶閲岋紱鍚屾椂璋冪敤瀹樻柟 smb_mgr.sh 璁╁悓涓€涓处鍙锋寕澶氫釜鍏变韩鐩綍锛岀粫寮€鍏变韩 app 鍙兘閰嶄竴涓殑闄愬埗銆?,
        "project": "xiaomi-netneighbor-plugin",
        "pluginId": 11012,
        "port": 18190,
        "releaseRoot": "/data/plugin/netneighbor",
        "uiKey": "netneighbor",
        "iconSource": "web/assets/netneighbor-icon.png",
        "iconName": "netneighbor.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "wsd.py": "wsd.py",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-netneighbor.service",
        "service": "xiaomi-netneighbor.service",
        "nginxSource": "deploy/xiaomi-netneighbor.nginx.conf",
        "nginx": "xiaomi-netneighbor.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "network"],
        "author": "community",
        "registry": {
            "icon": "/icon/netneighbor.icon?v=0.1.0",
            "frontend": {
                "title": "缃戠粶閭诲眳",
                "desc": "缃戠粶鍙鎬т笌鍏变韩鐩綍",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/netNeighbor_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/netNeighbor_pc"},
                ],
                "sortid": 11012,
                "widget": [],
            },
            "info": {"tags": ["tool", "network"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "nasconsole",
        "name": "鎺у埗鍙?,
        "version": "0.1.1",
        "summary": "妗岄潰寮忔帶鍒跺彴锛氱‖浠剁姸鎬併€佺‖鐩樹笌 SMART銆佹枃浠躲€佸鍣ㄤ笌鏈嶅姟",
        "description": "鐢佃剳娴忚鍣ㄦ墦寮€灏辨槸涓€寮犳闈細鍥炬爣 + 娴姩绐楀彛 + 甯搁┗缁勪欢鏍忥紝鐪?CPU/鍐呭瓨/娓╁害/缃戠粶/"
                       "纾佺洏 IO 鏇茬嚎銆佺‖鐩樹紤鐪犵姸鎬佷笌 SMART銆乵d 闃靛垪銆丏ocker 瀹瑰櫒銆佸凡瑁呮彃浠朵笌鍘傚晢瀹氭椂浠诲姟锛?
                       "鏂囦欢绐楀彛鍙祻瑙堛€侀瑙堛€佷笅杞斤紝涔熻兘涓婁紶銆佹柊寤烘枃浠跺す銆侀噸鍛藉悕銆佸垹闄わ紙鍒犻櫎杩涘洖鏀剁珯锛屽彲鎭㈠锛夈€?
                       "鎵嬫満绔彧鏈夊紑鍏炽€佺鍙ｄ笌浠ょ墝銆傞噰闆嗗彧璇?/proc 涓?/sys锛宻tandby 纭洏涓嶈 SMART锛屼笉鍞ら啋纭洏銆?,
        "project": "xiaomi-nas-console",
        "pluginId": 11020,
        "port": 18100,
        "releaseRoot": "/data/plugin/xiaomi-nas-console",
        "uiKey": "nasconsole",
        "iconSource": "assets/xiaomi-nas-console.png",
        "iconName": "nas-console.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "VERSION": "VERSION",
            "web": "web",
            # 妗岄潰鍏ュ彛鐨?nginx 妯℃澘锛氳鏈哄悗鏀惧湪鎻掍欢鑷繁鐨勭洰褰曢噷锛孉pp 閲屾敼绔彛/寮€鍏虫椂鐢ㄥ畠閲嶆柊娓叉煋
            "deploy/xiaomi-nas-console-lan.nginx.conf": "lan/xiaomi-nas-console-lan.conf",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-nas-console.service",
        "service": "xiaomi-nas-console.service",
        "nginxSource": "deploy/xiaomi-nas-console.nginx.conf",
        "nginx": "xiaomi-nas-console.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "monitoring"],
        "author": "Kingwell Community",
        "registry": {
            "icon": "/icon/nas-console.icon?v=1",
            "frontend": {
                "title": "鎺у埗鍙?,
                "desc": "纭欢鐘舵€併€佹枃浠躲€佸鍣ㄤ笌鏈嶅姟",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/nasConsole_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/nasConsole_pc"},
                ],
                "sortid": 11020,
                "widget": [],
            },
            "info": {
                "tags": ["tool", "monitoring"],
                "desc": "纭欢鐘舵€併€佺‖鐩樸€佸鍣ㄤ笌鏈嶅姟锛涘彲娴忚鍜屾暣鐞嗘枃浠讹紙鍒犻櫎杩涘洖鏀剁珯锛?,
                "developer": "Kingwell Community",
                "publisher": "community",
                "ext": {"admin": True},
            },
        },
    },
    {
        "id": "rtrcenter",
        "name": "Unifi",
        "version": "0.1.3",
        "summary": "鎶婂眬鍩熺綉閲岀殑 UniFi SoftCenter 鎼繘灏忕背瀛樺偍锛屾墜鏈?App 杩滅▼绠＄悊璺敱鍣ㄦ彃浠?,
        "description": "鍦ㄥ皬绫虫櫤鑳藉瓨鍌ㄧ殑鎵嬫満 App 閲岀洿鎺ユ墦寮€璺敱鍣ㄤ笂鐨?UniFi SoftCenter锛岃鎻掍欢銆佺湅鏃ュ織銆?
                       "鏀瑰畾鏃朵换鍔°€侀噸鍚湇鍔★細鎻掍欢鍦?NAS 涓婂仛鍙嶅悜浠ｇ悊锛岃蒋浠朵腑蹇冪殑椤甸潰涓庢帴鍙ｉ兘缁?NAS 鐨?
                       "瀹㈡埛绔叆鍙ｏ紙瀹㈡埛绔瘉涔︼級杞彂鍒拌矾鐢卞櫒锛屽洜姝?*涓嶉渶瑕佺粰璺敱鍣ㄥ仛绔彛鏄犲皠**锛?
                       "璺敱鍣ㄤ篃涓嶄細鏆撮湶鍒板叕缃戙€備负閫傞厤 App 鍐呯疆 webview锛堥€氬父鍙厑璁歌闂?NAS 鑷繁鐨勫煙鍚嶏級锛?
                       "鎻掍欢鎶婅蒋浠朵腑蹇冧緷璧栫殑 Vue/Tailwind/Lucide 鎹㈡垚鏈湴鍓湰銆佹妸椤甸潰閲岀洿杩?GitHub 鐨?
                       "鍦板潃鏀圭粡 NAS 杞彂锛屽苟鍦ㄥ乏涓婅鎻愪緵鍏抽棴鎸夐挳銆佸彸涓婅鎻愪緵璁剧疆椤碉紙鏀圭洰鏍囧湴鍧€涓庣湅鐘舵€侊級銆?
                       "AdminToken 瀛樺湪 NAS 涓婏紙0600锛夛紝鎵撳紑杞欢涓績鏃惰嚜鍔ㄦ敞鍏ワ紝鎹㈣澶囦笉鐢ㄩ噸杈撱€?,
        "project": "xiaomi-router-center",
        "pluginId": 11021,
        "port": 18101,
        "releaseRoot": "/data/plugin/router-center",
        "uiKey": "rtrcenter",
        "iconSource": "assets/xiaomi-router-center.png",
        "iconName": "router-center.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "VERSION": "VERSION",
            "web": "web",
            # 瀹㈡埛绔叆鍙ｇ殑 nginx 妯℃澘锛氭湇鍔℃敼鐩爣鍦板潃鏃剁敤瀹冮噸鏂版覆鏌擄紙娓叉煋鍚?nginx -t 杩囧叧鎵?reload锛?
            "deploy/xiaomi-router-center.nginx.conf": "deploy/xiaomi-router-center.nginx.conf",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-router-center.service",
        "service": "xiaomi-router-center.service",
        "nginxSource": "deploy/xiaomi-router-center.nginx.conf",
        "nginx": "xiaomi-router-center.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "network"],
        "author": "Kingwell Community",
        "registry": {
            "icon": "/icon/router-center.icon?v=1",
            "frontend": {
                "title": "Unifi",
                "desc": "杩滅▼绠＄悊 UniFi 杞欢涓績",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/routerCenter_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/routerCenter_pc"},
                ],
                "sortid": 11021,
                "widget": [],
            },
            "info": {
                "tags": ["tool", "network"],
                "desc": "鎶婂眬鍩熺綉閲?UniFi SoftCenter 鎼繘灏忕背瀛樺偍锛屾墜鏈?App 杩滅▼绠＄悊璺敱鍣ㄦ彃浠?,
                "developer": "Kingwell Community",
                "publisher": "community",
                "ext": {"admin": True},
            },
        },
    },
    {
        "id": "dockerctl",
        "name": "Docker 鎺у埗鍙?,
        "version": "0.1.0",
        "summary": "瀹屾暣 Docker 鎺у埗鍙帮細瀹瑰櫒銆侀暅鍍忋€丆ompose 閮ㄧ讲銆佸瓨鍌ㄥ嵎涓庣綉缁滅鐞嗭紱闇€瑕?Docker",
        "description": "瀹瑰櫒鍚仠/鏃ュ織/璇︽儏銆侀暅鍍忔媺鍙栵紙甯﹁繘搴︿笌鍙栨秷锛変笌鍒犻櫎銆佽〃鍗曢儴缃蹭笌 Compose 閮ㄧ讲銆?
                       "瀛樺偍鍗蜂笌缃戠粶绠＄悊锛屾搷浣滄棩蹇楄惤鐩樸€侱ocker CLI 榛樿 /data/docker/docker锛屽彲鍦ㄦ彃浠堕〉淇敼锛?
                       "绔彛 18210 浠呭洖鐜紝鍙帴鍙楀皬绫冲鎴风璇佷功鍏ュ彛銆?,
        "project": "xiaomi-dockerctl-plugin",
        "pluginId": 11022,
        # 18210锛氱幇鏈夌ぞ鍖烘彃浠朵緷娆″崰 18080/18100/18101/18110/18115/18117/18120/18122/
        # 18130/18140/18150/18160/18170/18180/18190锛?8210 鍦ㄤ粨搴撻噷鏈嚭鐜帮紙宸插叏閲?grep锛夈€?
        "port": 18210,
        "releaseRoot": "/data/plugin/dockerctl",
        "uiKey": "dockerctl",
        "iconSource": "web/assets/dockerctl.png",
        "iconName": "dockerctl.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "VERSION": "VERSION",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-dockerctl.service",
        "service": "xiaomi-dockerctl.service",
        "nginxSource": "deploy/xiaomi-dockerctl.nginx.conf",
        "nginx": "xiaomi-dockerctl.conf",
        "healthPath": "/api/health",
        "tags": ["tool", "docker"],
        "author": "community",
        "registry": {
            "icon": "/icon/dockerctl.icon?v=0.1.0",
            "frontend": {
                "title": "Docker 鎺у埗鍙?,
                "desc": "瀹瑰櫒涓庨暅鍍忕鐞?,
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/dockerctl_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/dockerctl_pc"},
                ],
                "sortid": 11022,
                "widget": [],
            },
            "info": {"tags": ["tool", "docker"], "publisher": "community", "ext": {"admin": True}},
        },
    },
]


def _attach_plugin_elements() -> None:
    """鎶婂悇鎻掍欢鑷甫鐨勫皬绫虫彃浠惰鑼冭绱犵撼鍏ユ墦鍖呭唴瀹广€?

    姣忎釜鎻掍欢鍙湪鑷繁鐨?deploy/ 涓嬫彁渚涳細
      plugin-meta.json  鈥斺€?INFO 鐨勫厓鏁版嵁锛堝悕绉般€佹弿杩般€佹爣绛剧瓑锛?
      control           鈥斺€?plugincenter boot 鏃舵墽琛岀殑 <plugin>/scripts/control
    瀹夎鏃朵細澶嶅埗鍒版彃浠剁洰褰曪紝浣?plugin.sh verify 閫氳繃銆?
    """
    elements = (
        ("deploy/plugin-meta.json", "plugin-meta.json"),
        ("deploy/control", "control"),
    )
    for spec in PACKAGE_SPECS:
        project = PROJECTS / spec["project"]
        for source, destination in elements:
            if (project / source).is_file():
                spec["runtime"].setdefault(source, destination)


_attach_plugin_elements()


def copy_required(source: Path, target: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Missing package source: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def write_deterministic_zip(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 9, 2, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def sync_project_version(spec: dict[str, Any]) -> None:
    """鎶婃渶缁堢増鏈彿鍐欏洖鎻掍欢鑷繁鐨?VERSION 鏂囦欢锛堝瓨鍦ㄦ墠鍐欙級銆?

    鎻掍欢杩愯鏃朵細璇诲畨瑁呯増鏈紙鍟嗗簵瑁呭埌 `releases/<娓呭崟鐗堟湰>-<鏃堕棿鎴?-<pid>/`锛?
    鑴氭湰瀹夎璇?VERSION 鏂囦欢锛夛紝濡傛灉鍖呴噷 VERSION 鐨勫唴瀹瑰拰娓呭崟鐗堟湰涓嶄竴鑷达紝
    鎻掍欢椤垫樉绀虹殑鐗堟湰灏变細鍜屽晢搴?娉ㄥ唽琛ㄥ涓嶄笂鈥斺€旇瘎瀹℃椂韪╄繃杩欎釜鍧戯紝鎵€浠ュ湪杩欓噷瀵归綈銆?
    """
    version_file = PROJECTS / spec["project"] / "VERSION"
    if not version_file.is_file():
        return
    current = version_file.read_text(encoding="utf-8").strip()
    if current == spec["version"]:
        return
    version_file.write_text(spec["version"] + "\n", encoding="utf-8", newline="\n")
    print(f"  鍚屾 {version_file.relative_to(ROOT)}: {current} 鈫?{spec['version']}")


def build_bundle(spec: dict[str, Any]) -> dict[str, Any]:
    project = PROJECTS / spec["project"]
    if not project.is_dir():
        raise SystemExit(f"Project directory not found: {project}")

    with tempfile.TemporaryDirectory(prefix=f"apps-{spec['id']}-") as temporary:
        root = Path(temporary)
        for source, destination in spec["runtime"].items():
            copy_required(project / source, root / "runtime" / destination)
        copy_required(project / spec["ui"], root / "ui")
        copy_required(project / spec["iconSource"], root / "icon")
        copy_required(project / spec["serviceSource"], root / "config" / spec["service"])
        copy_required(project / spec["nginxSource"], root / "config" / spec["nginx"])
        manifest = {
            "schemaVersion": 1,
            "id": spec["id"],
            "name": spec["name"],
            "version": spec["version"],
            "pluginId": spec["pluginId"],
            "port": spec["port"],
            "paths": {
                "releaseRoot": spec["releaseRoot"],
                "uiKey": spec["uiKey"],
                "iconName": spec["iconName"],
            },
            "service": spec["service"],
            "nginx": spec["nginx"],
            "healthPath": spec["healthPath"],
            "registry": spec["registry"],
        }
        if spec.get("requirements"):
            manifest["requirements"] = spec["requirements"]
        # 鏄惧紡 LF锛氳繖涓枃浠惰繘 zip锛學indows 涓婂啓鎴?CRLF 浼氳鍚屼竴涓寘绠楀嚭涓嶅悓鐨?
        # sha256锛孋I 灏变細鎶娿€屽唴瀹规病鍙樸€嶈鍒ゆ垚銆屽唴瀹规湁鍙樸€嶈€屽嚟绌烘姮涓€涓増鏈€?
        with (root / "manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        bundle_name = f"{spec['id']}-{spec['version']}.zip"
        bundle_path = APPS / bundle_name
        write_deterministic_zip(root, bundle_path)

    icon_name = f"{spec['id']}.png"
    copy_required(project / spec["iconSource"], APPS / "icons" / icon_name)
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    size = bundle_path.stat().st_size
    channel = "candidate" if "-rc" in spec["version"] or "-beta" in spec["version"] else "stable"
    return {
        "id": spec["id"],
        "name": spec["name"],
        "version": spec["version"],
        "summary": spec["summary"],
        "description": spec.get("description", spec["summary"]),
        "bundle": f"apps/{bundle_name}",
        "sha256": digest,
        "size": size,
        "icon": f"apps/icons/{icon_name}",
        "channel": channel,
        "tags": spec.get("tags", []),
        "author": spec.get("author", "community"),
        "pluginId": spec["pluginId"],
        "port": spec["port"],
    }


def detect_repo_slug() -> str:
    """Best-effort owner/name from git remote; falls back to a placeholder."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "fw867/xiaomi-nas-plugin-market"
    for prefix in ("git@github.com:", "https://github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix):].removesuffix(".git").strip("/")
            if slug.count("/") == 1:
                return slug
    return "fw867/xiaomi-nas-plugin-market"


def read_store_version() -> str:
    release_script = PROJECTS / "xiaomi-community-app-store" / "scripts" / "build_release.py"
    if not release_script.is_file():
        return "0.2.0"
    for line in release_script.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERSION"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.2.0"


def load_existing_versions() -> dict[str, str]:
    """浠庣幇鏈?apps.json 璇诲彇鍚勬彃浠剁殑褰撳墠鐗堟湰鍙枫€?""
    if not APPS_JSON.is_file():
        return {}
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
        return {a["id"]: a["version"] for a in data.get("apps", []) if "id" in a and "version" in a}
    except (json.JSONDecodeError, OSError):
        return {}


def load_existing_hashes() -> dict[str, str]:
    """浠庣幇鏈?apps.json 璇诲彇鍚勬彃浠跺凡鍙戝竷鍖呯殑 SHA-256锛岀敤浜庡垽鏂唴瀹规槸鍚﹀彉鍖栥€?""
    if not APPS_JSON.is_file():
        return {}
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
        return {
            a["id"]: a["sha256"]
            for a in data.get("apps", [])
            if "id" in a and isinstance(a.get("sha256"), str)
        }
    except (json.JSONDecodeError, OSError):
        return {}


def load_existing_generated_at() -> int:
    """璇诲彇鐜版湁 apps.json 鐨?generatedAt锛岀敤浜庡唴瀹规病鍙樻椂鍘熸牱淇濈暀銆?""
    if not APPS_JSON.is_file():
        return 0
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    value = data.get("generatedAt")
    return value if isinstance(value, int) else 0


def load_existing_apps() -> list[dict[str, Any]]:
    """璇诲彇鐜版湁 apps.json 涓殑搴旂敤鏉＄洰锛堢敤浜?--only 鏃朵繚鐣欏叾瀹冩潯鐩級銆?""
    if not APPS_JSON.is_file():
        return []
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
        apps = data.get("apps", [])
        return [a for a in apps if isinstance(a, dict) and "id" in a]
    except (json.JSONDecodeError, OSError):
        return []


def bump_patch(version: str) -> str:
    """閫掑 patch 鐗堟湰鍙凤細0.4.1 鈫?0.4.2锛?.2.0-rc5 鈫?0.2.1-rc5銆?""
    import re as _re
    m = _re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", version)
    if not m:
        return version
    major, minor, patch, suffix = m.groups()
    return f"{major}.{minor}.{int(patch) + 1}{suffix}"


def main() -> int:
    # 杈撳嚭缁熶竴璧?UTF-8锛歐indows 鎺у埗鍙伴粯璁?GBK 鏃讹紝鎵撳嵃 "鈿? 涔嬬被鐨勫瓧绗︿細鐩存帴
    # UnicodeEncodeError 鎶婃暣涓瀯寤烘墦鏂紙璺宠繃鎻掍欢鐨勫垎鏀濂戒細鎵撳嵃瀹冿級銆?
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Build apps/ bundles and apps.json")
    parser.add_argument("--only", help="Build a single app id")
    parser.add_argument("--include-candidates", action="store_true", help="Include rc/beta packages")
    parser.add_argument("--skip-missing", action="store_true", help="Skip apps with missing sources instead of failing")
    parser.add_argument("--no-bump", action="store_true", help="Do not auto-increment patch version")
    args = parser.parse_args()

    # 鐗堟湰鍙风瓥鐣ワ細榛樿娌跨敤 apps.json 閲屽凡鍙戝竷鐨勭増鏈紝鍙湁鎵撳寘鍐呭鐪熺殑
    # 鍙樹簡鎵嶉€掑 patch銆傚惁鍒欐瘡娆?CI 鏋勫缓閮戒細鎶婂叏閮ㄦ彃浠舵姮涓€涓増鏈紝
    # 瀹㈡埛绔細瀵规墍鏈夊凡瀹夎鎻掍欢鏄剧ず銆屽彲鏇存柊銆嶃€?
    existing_versions = load_existing_versions()
    existing_hashes = load_existing_hashes()
    if existing_versions and not args.no_bump:
        for spec in PACKAGE_SPECS:
            old = existing_versions.get(spec["id"])
            if old:
                spec["version"] = old

    specs = PACKAGE_SPECS
    if args.only:
        specs = [s for s in specs if s["id"] == args.only]
        if not specs:
            raise SystemExit(f"Unknown app id: {args.only}")
    if not args.include_candidates:
        skipped = [s["id"] for s in specs if "-rc" in s["version"] or "-beta" in s["version"]]
        if skipped:
            print(f"Skipping candidate packages (use --include-candidates): {', '.join(skipped)}")
        specs = [s for s in specs if "-rc" not in s["version"] and "-beta" not in s["version"]]

    APPS.mkdir(parents=True, exist_ok=True)
    (APPS / "icons").mkdir(parents=True, exist_ok=True)

    apps: list[dict[str, Any]] = []
    skipped_ids: list[str] = []
    for spec in specs:
        published = existing_versions.get(spec["id"])
        print(f"Building {spec['id']} {spec['version']} ...")
        # 鎵撳寘鍓嶅厛璁╂彃浠惰嚜宸辩殑 VERSION 鏂囦欢涓庢竻鍗曠増鏈竴鑷达紝鍖呴噷鐨勭増鏈彿鎵嶄笉浼氳嚜鐩哥煕鐩?
        if not args.no_bump:
            sync_project_version(spec)
        try:
            entry = build_bundle(spec)
        except SystemExit as error:
            if args.skip_missing:
                print(f"  鈿?skipped: {error}")
                skipped_ids.append(spec["id"])
                continue
            raise

        # 鍐呭涓庡凡鍙戝竷鍖呬竴鑷?鈫?淇濇寔鐗堟湰鍙凤紝瀹㈡埛绔笉浼氭彁绀烘洿鏂般€?
        # 鍙湁鍐呭鐪熺殑鍙樹簡鎵嶉€掑 patch 骞堕噸鏂版墦鍖呫€?
        if published and not args.no_bump:
            if existing_hashes.get(spec["id"]) == entry["sha256"]:
                print("  鍐呭鏈彉锛屼繚鎸佺増鏈?)
            else:
                bumped = bump_patch(published)
                spec["version"] = bumped
                print(f"  鍐呭鏈夊彉锛岀増鏈€掑 {published} 鈫?{bumped}")
                sync_project_version(spec)
                entry = build_bundle(spec)

        apps.append(entry)
        print(f"  鈫?{entry['bundle']}  sha256={entry['sha256'][:16]}鈥? {entry['size']//1024} KiB")

    if not apps:
        raise SystemExit("No apps were built")

    # 娓呯悊琚噸寤哄簲鐢ㄧ殑鏃х増鏈?zip锛堟寜鏈€缁堢増鏈彿鍒ゆ柇锛屼繚鐣欏叾瀹冨簲鐢ㄧ殑鍖咃級
    keep_names = {Path(entry["bundle"]).name for entry in apps}
    built_ids = {entry["id"] for entry in apps}
    for old_zip in APPS.glob("*.zip"):
        if old_zip.name in keep_names:
            continue
        if any(old_zip.name.startswith(f"{app_id}-") for app_id in built_ids):
            old_zip.unlink()
            print(f"娓呯悊鏃у寘: {old_zip.name}")

    # 浣跨敤 --only 鏃朵繚鐣欐湭閲嶅缓搴旂敤鐨勬棦鏈夋潯鐩紝閬垮厤娓呭崟琚竻绌恒€?
    # 椤哄簭鎸?PACKAGE_SPECS 鑰屼笉鏄?id 鎺掑簭锛氬叏閲忔瀯寤哄氨鏄繖涓『搴忥紝涓よ€呬笉涓€鑷?
    # 浼氳娓呭崟鍦ㄦ瘡娆℃瀯寤轰箣闂存潵鍥為噸鎺掋€?
    if args.only:
        rebuilt = {entry["id"]: entry for entry in apps}
        published = {app["id"]: app for app in load_existing_apps()}
        apps = [rebuilt.get(spec["id"]) or published.get(spec["id"]) for spec in PACKAGE_SPECS]
        apps = [entry for entry in apps if entry]

    repo = detect_repo_slug()
    branch = "main"
    # 鍐呭鍏ㄩ兘娌″彉鏃朵笉瑕佸埛鏂?generatedAt锛氬惁鍒?CI 姣忔杩愯閮戒細浜х敓涓€涓彧鏀?
    # 鏃堕棿鎴崇殑鎻愪氦锛堝氨鏄偅涓?"chore: auto-build apps"锛夛紝鏈湴鍚庣画鏀瑰姩闅忓嵆涓?
    # 杩滅鍒嗗弶锛屼簬鏄瘡娆℃帹閫佸墠閮藉緱鍏?pull --rebase 涓€閬嶃€?
    content_changed = (
        len(apps) != len(existing_hashes)
        or any(existing_hashes.get(entry["id"]) != entry["sha256"] for entry in apps)
    )
    previous_generated_at = load_existing_generated_at()
    if content_changed or not previous_generated_at:
        generated_at = int(time.time())
    else:
        generated_at = previous_generated_at
        print("鍐呭涓庡凡鍙戝竷鐗堟湰涓€鑷达紝淇濇寔 generatedAt 涓嶅彉锛堜笉浜х敓绌烘彁浜わ級")
    apps_doc = {
        "schemaVersion": 2,
        "name": "灏忕背鏅鸿兘瀛樺偍搴旂敤鍟嗗簵",
        "generatedAt": generated_at,
        "repository": repo,
        "branch": branch,
        "store": {
            "version": read_store_version(),
            "releasesUrl": f"https://github.com/{repo}/releases",
            "latestReleaseApi": f"https://api.github.com/repos/{repo}/releases/latest",
        },
        "apps": apps,
    }
    # 鏄惧紡鍐?LF锛?gitattributes 鐢?`* -text` 鍏虫帀浜嗚灏惧綊涓€鍖栵紝Windows 涓婇粯璁?
    # 鍐欏嚭鐨?CRLF 浼氳鏁翠唤娓呭崟鍦?diff 閲屽叏閲忓彉鍖栵紝涔熷拰 CI/Linux 鐨勪骇鐗╀笉涓€鑷淬€?
    with APPS_JSON.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(apps_doc, ensure_ascii=False, indent=2) + "\n")
    print(f"\nWrote {APPS_JSON.relative_to(ROOT)} with {len(apps)} apps")
    if skipped_ids:
        print(f"Skipped: {', '.join(skipped_ids)}")
    print(f"Repository: {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
