# 缃戠粶閭诲眳锛坣etneighbor锛?
瑙ｅ喅涓や釜鏃ュ父鐥涚偣锛?
1. **Windows 璧勬簮绠＄悊鍣ㄣ€岀綉缁溿€嶉噷鐪嬩笉鍒板皬绫冲瓨鍌?*锛屾瘡娆″彧鑳芥墜杈?`\\192.168.1.8`锛?2. **鍏变韩 app 閲屼竴涓处鍙峰彧鑳介厤涓€涓叡浜洰褰?*锛屾兂鍐嶅叡浜竴涓枃浠跺す娌℃湁鍏ュ彛銆?
涓や欢浜嬮兘涓嶆槸纭欢鎴栧唴鏍哥殑闄愬埗锛氱涓€浠舵槸绯荤粺鑷甫鐨勫彂鐜版湇鍔★紙`/usr/bin/wsdd`锛夊彂鐨勬姤鏂?灏戜簡涓€涓繀闇€瀛楁銆佸厓鏁版嵁鎺ュ彛涔熷彧璁?GET锛涚浜屼欢鏇寸畝鍗曗€斺€斿簳灞傛湰鏉ュ氨鏀寔涓€涓处鍙锋寕澶氫釜
鐩綍锛屽彧鏄?app 鐨勭晫闈笉缁欍€傛湰鎻掍欢鎶婅繖涓ゅ琛ラ綈銆?
## 涓€銆佷负浠€涔堣嚜宸卞疄鐜?WSD 鍥炲簲鍣?
Windows 鐨勩€岀綉缁溿€嶉潬 **WSD锛圵S-Discovery锛?* 鎵捐澶囥€傜郴缁熼噷鐨?`/usr/bin/wsdd` 鐪嬭捣鏉ュ湪
姝ｅ父宸ヤ綔锛氳兘鍥炵瓟 Probe銆佸惎鍔ㄦ椂鍙?Hello銆佸仠姝㈡椂鍙?Bye锛宍ss` 涔熻兘鐪嬪埌瀹冪洃鍚?3702銆備絾瀹冩湁
涓夊鑷村懡缂哄彛锛?*浠讳綍涓€涓兘浼氳 Windows 瀹屽叏鏃犺杩欏彴涓绘満**锛?026-09-28 鍦?NAS 涓婁笌涓婃父
[wsdd](https://github.com/christgau/wsdd) 鍋氬瓧鑺傜骇瀵规瘮瀹炴祴寰楀嚭锛夛細

| # | 缂哄彛 | 瀹炴祴鐜拌薄 |
| --- | --- | --- |
| 1 | 娑堟伅**娌℃湁 `wsd:AppSequence` 澶?* | WS-Discovery 瑙勫畾 Hello / Bye / ProbeMatches / ResolveMatches 蹇呴』甯﹀畠銆傜己浜嗕箣鍚?Windows 鏀跺埌 ProbeMatch 涔?*鏁存潯涓㈡帀**锛岃繛璁惧鎻忚堪閮戒笉鏉ュ彇锛堟姄鍖呭彲瑙侊細鍙湁鎴戜滑鐨?Probe 鍦ㄥ彂锛孨AS 渚ф敹涓嶅埌浠讳綍 HTTP 璇锋眰锛?|
| 2 | 鍏冩暟鎹帴鍙?*涓嶅搷搴?`WS-Transfer Get`锛圫OAP POST锛?* | `curl -X POST http://192.168.1.8:35169/wsd/` 鈫?`(52) Empty reply from server`锛涜€屽悓涓€鍦板潃 `curl`锛圙ET锛夎繑鍥?`200`銆?175 瀛楄妭銆俉indows 鍙栨弿杩扮敤鐨勬鏄偅涓?POST |
| 3 | 鎻忚堪鍝嶅簲**娌℃湁 `wsa:RelatesTo`** | 琛ヤ笂 1 涔嬪悗 Windows 浼氭潵鍙栧厓鏁版嵁锛堟湇鍔＄鑳界湅鍒?`POST 鈥?action=Get`锛夛紝浣嗙己浜嗚繖鏉″叧鑱?ID 瀹冧細涓㈠純鏁翠唤鎻忚堪鈥斺€旇〃鐜颁负鈥滄棩蹇楅噷鏄庢槑 200 浜嗭紝涓绘満杩樻槸涓嶅嚭鐜扳€?|

琛ラ綈涓夋潯涔嬪悗锛屾湰鏈?Windows 鐨勮祫婧愮鐞嗗櫒銆岀綉缁溿€嶉噷绔嬪埢鍑虹幇 `SMARTSTORAGE`锛屽弻鍑诲彲鐩存帴
杩涘叆 SMB 鍏变韩锛涘彂 `Bye` 鏃跺畠浼氭秷澶便€佸啀 `Hello` 鍙堝洖鏉ワ紙瀵瑰簲鎻掍欢鐨勫仠姝?鍚姩璺緞锛夈€?
鎵€浠?`wsd.py` 鑷繁瀹炵幇浜嗗畬鏁寸殑鏈嶅姟绔竴渚э細

- 缁勬挱鐩戝惉 `3702`锛歚Probe` 鈫?`ProbeMatches`锛宍Resolve` 鈫?`ResolveMatches`锛?- 涓诲姩閫氬憡锛氬惎鍔?`Hello`锛堝彂涓ら亶锛孶DP 浼氫涪锛夈€侀€€鍑?`Bye`銆佹瘡 `WSD_HELLO_INTERVAL`
  绉掞紙榛樿 900锛夐噸鍙戜竴娆★紱
- 鍏冩暟鎹?HTTP 鏈嶅姟锛堥粯璁?`5357`锛夛細鏅€?GET 涓?`WS-Transfer Get` 閮借繑鍥?`wsx:Metadata`锛?  骞舵寜璇锋眰鍥炲～ `wsa:RelatesTo`锛?- 韬唤 `urn:uuid` 钀藉湪 `<DATA_DIR>/wsd.json`锛岄噸鍚笉鍙樷€斺€斿惁鍒?Windows 閲屼細澶氬嚭骞界伒璁惧锛?- 缁嗚妭锛氬搷搴?*浠?3702 绔彛鍙戝嚭**锛涘彂閫佸墠鐢?`IP_MULTICAST_IF` 鎶婄粍鎾嚭鍙ｉ拤鍦ㄥ眬鍩熺綉缃戝崱涓?  锛堜笉閽夌殑璇濆彲鑳戒粠 `docker0` 鍑哄幓锛屼竴鍙版満鍣ㄩ兘鐪嬩笉鍒帮紝NAS 涓婂疄娴嬭俯杩囷級銆?
## 浜屻€佷负浠€涔堟帴绠″畼鏂?wsdd 鐢?drop-in 鑰屼笉鏄?`systemctl mask`

涓や釜鍙戠幇鏈嶅姟浼氭姠 3702锛屽繀椤昏瀹樻柟閭ｄ釜鍋滀笅鏉ャ€備絾**涓嶈兘鐢?`mask`**锛?
瀹樻柟鐨?`/usr/bin/smb_mgr.sh`锛堝叡浜?app 鐨勫悗绔級姣忔淇濆瓨鍏变韩锛屾渶鍚庝竴姝ラ兘浼?`systemctl restart wsdd`銆俙mask` 涔嬪悗杩欎竴姝ュ繀鐒跺け璐ワ紝鏁存潯鍛戒护杩斿洖 1鈥斺€旂敤鎴峰湪鍏变韩 app 閲?灏变細鐪嬪埌銆屼繚瀛樺け璐ャ€嶏紝灏界 `smb.conf` 鍏跺疄宸茬粡鍐欏ソ浜嗐€傝繖鏄彲瑙佺殑鍔熻兘鍥炲綊銆?
鏀规垚鍐欎竴涓┖鎿嶄綔鐨?drop-in锛堝彧鍔ㄦ垜浠嚜宸辩殑閭ｄ釜鏂囦欢锛屼笉纰?`/usr/lib` 涓嬬殑鍘熷崟鍏冿級锛?
```ini
# /etc/systemd/system/wsdd.service.d/netneighbor.conf
[Service]
ExecStart=
ExecStart=/bin/true
ExecStop=
ExecStop=/bin/true
ExecReload=
ExecReload=/bin/true
RemainAfterExit=yes
```

杩欐牱 `systemctl restart wsdd` 渚濇棫杩斿洖 0锛岃€?3702 琚湡姝ｉ噴鏀俱€傛帴绠￠『搴忔槸
`stop wsdd` 鈫?`pkill -x wsdd` 鍏滃簳 鈫?鍐?drop-in 鈫?`daemon-reload` 鈫?鑷 3702 鏄惁鐪熺殑
绌哄嚭鏉ワ紙鍗犵敤鏃舵棩蹇椾細鎵撳嵃鎸佹湁杩涚▼锛屼究浜庢帓鏌ワ級銆?
**鍗歌浇/鍋滅敤鏃惰繕鍘?*锛氬垹鎺?drop-in 鈫?`daemon-reload` 鈫?`systemctl start wsdd`銆?鏈嶅姟鍗曞厓閲岃繕鏈変竴鏉?`ExecStopPost=鈥?server.py --restore-wsdd`锛屼綔涓鸿繘绋嬭 `kill -9`
鏃剁殑绗簩閬撲繚闄┿€備袱姝ラ兘鏄箓绛夌殑銆?
## 涓夈€佷竴涓处鍙峰涓叡浜洰褰?
灏忕背鍏变韩 app 鐨勬暟鎹簮鏄?UCI锛?
```
/etc/config/sambauser    # 姣忎釜璐﹀彿涓€娈碉細option user 'u3943892'锛圢AS 鐢ㄦ埛锛?                         #                 option name 'fw867'锛堣处鍙凤級
                         #                 list dirs '<鐩綍>'锛堟湰鏉ュ氨鏄垪琛級
/etc/config/sambashare   # 姣忎釜鍏变韩涓€娈碉細option name / path / list users 'samba<璐﹀彿>'
/etc/samba/users.map     # samba<璐﹀彿> = <璐﹀彿>
```

搴曞眰涓€鐩存槸銆屼竴涓处鍙?鈫?澶氫釜 dirs 鈫?澶氫釜 sambashare 娈点€嶏紝鍙槸 app 鍙啓涓€涓€傚畼鏂?`/usr/bin/smb_mgr.sh` 涔熺暀浜嗗懡浠わ細

```
smb_mgr.sh shares add_dir <share_name> <path> "<璐﹀彿>" <鍏变韩鍚? [force_user]
smb_mgr.sh shares del_dir <share_name>
```

瀹炴祴锛堝悓涓€璐﹀彿 `fw867`锛孲MB 鐢ㄦ埛 `sambafw867`锛夛細

```
$ smb_mgr.sh shares add_dir u3943892_nb_1 '/home/u3943892/pool0/data/鎴戠殑鐓х墖' 'fw867' '鐓х墖-3943892' 'u3943892'
$ testparm -s | grep -A3 鐓х墖-3943892
[鐓х墖-3943892]  path = "/home/u3943892/pool0/data/鎴戠殑鐓х墖"  valid users = sambafw867
$ net view \\192.168.1.8          # 鍚屼竴璐﹀彿涓嬪嚭鐜颁袱涓叡浜?  锟斤拷锟?3943892  Disk   u3943892_1 share directory
  锟斤拷片-3943892  Disk   u3943892_nb_1 share directory
```

涓や釜瀹规槗韪╃殑鍧戯紙閮藉凡瀹炴祴锛屽埆鍐嶆敼鍥炲幓锛夛細

- `<user_list>` 瑕佷紶**璐﹀彿鍚?*锛坄fw867`锛夛紝`smb_mgr.sh` 鍐呴儴鑷繁鍔?`samba` 鍓嶇紑锛?  浼?`sambafw867` 浼氬湪 `pdbedit` 閭ｆ鎶?`Username not found!` 骞朵互 1 閫€鍑恒€?- **涓嶈璋冪敤 `smb_mgr.sh reload`/`restart`**锛氬畠浠渶鍚庝竴姝ユ槸 `systemctl restart wsdd`锛?  鍦ㄦ帴绠′箣鍚庯紙鍝€曠敤 drop-in锛変篃鍙兘鎷垮埌闈?0锛岃浜鸿鍒ゅけ璐ャ€傛彃浠舵敼鎴愯嚜宸辫蛋
  `smb_mgr.sh init_config` + `systemctl reload smb nmb`锛岀劧鍚?*鍥炶 `/etc/config/sambashare`
  涓?`/var/etc/smb.conf` 纭鍏变韩鐪熺殑鍐欒繘鍘讳簡**锛屼互姝ゅ垽瀹氭垚鍔熴€?
鎻掍欢鑷缓鐨勫叡浜敤鐙珛鍛藉悕绌洪棿 `<璐﹀彿>_nb_<搴忓彿>`锛坄SHARE_PREFIX = '_nb_'`锛夛紝鍙厑璁稿鍒?鑷繁寤虹殑锛歛pp 鐢熸垚鐨?`<璐﹀彿>_<id>` 涓?`public` 绛夊彈淇濇姢鍏变韩涓嶄細琚彃浠剁銆?
### 澶栨帴璁惧锛歚/nas/mnt` 涓嬬殑鐩綍涔熻兘鍏变韩

`/nas/mnt` 涓嬮潰鏄悇绉嶆寕杞界偣锛坄usb` 鏄鎺ヨ澶?U 鐩樼殑绋冲畾璺緞锛孨AS 涓婄殑鏈嶅姟鎶婂畠 bind 鍒?鍘傚晢鑷繁鐨?`/mnt/usb-<鍝堝笇>`锛涜繕鏈?`pa0`/`pa1` 涔嬬被锛夈€傛彃浠堕粯璁ゆ妸 `/nas/mnt` 浣滀负
**杩藉姞**鐨勫厑璁告牴锛堢幆澧冨彉閲?`EXTRA_ROOTS`锛屽啋鍙峰垎闅斿涓€佹敮鎸佸崟灞傞€氶厤濡?`/mnt/usb-*`锛夛紝鎵€浠?`/nas/mnt/usb` 鍙婂叾涓嬮潰鐨勪换鎰忓瓙鐩綍閮借兘閫氳繃鏍￠獙骞惰鍏变韩锛?
```
鏈€缁堝厑璁告牴 = 锛圓LLOWED_ROOTS 鏈夊垯鍗曠嫭鐢ㄥ畠锛屽惁鍒?FALLBACK_ROOTS + 鎸夐厤缃淳鐢燂級+ EXTRA_ROOTS
```

涓ょ偣绾﹀畾锛?
- `ALLOWED_ROOTS` 浠嶇劧鏄?*鏄惧紡瑕嗙洊**璇箟锛堢粰浜嗗氨鍙敤瀹冿級锛宍EXTRA_ROOTS` 鍙槸杩藉姞锛?- 杩藉姞鏍?*鍙垪褰撳墠鐪熷疄瀛樺湪鐨?*锛氭嫈鐩樺悗 `/nas/mnt/usb` 涓嶅瓨鍦紝灏变笉浼氬嚭鐜板湪鐧藉悕鍗曢噷锛?  `allowed_roots()` 鐨勭紦瀛樻妸銆屽綋鍓嶇殑杩藉姞鏍瑰垪琛ㄣ€嶄篃绠楄繘鏈夋晥鏈燂紝鎻掍笂/鎷旀帀 U 鐩樹笅涓€娆¤皟鐢?  灏辫兘鍙嶆槧鍑烘潵锛堜笉蹇呯瓑缂撳瓨杩囨湡锛夈€傚凡鍏变韩鐨?U 鐩樼洰褰曞湪鎷旂洏鍚庝粛鐒朵繚鐣欏湪閰嶇疆閲岋紙涓嶆姤閿欙級锛?  鍙槸寮圭獥閲岄偅涓綅缃樉绀恒€屾湭鎺ュ叆銆嶃€?
`EXTRA_ROOTS` 鐨勪笁绉嶅彇鍊硷細

| 鍙栧€?| 琛屼负 |
| --- | --- |
| 鏈缃?| 榛樿 `/nas/mnt`锛堝紑绠卞嵆鍙叡浜鎺ヨ澶囬噷鐨勭洰褰曪級 |
| **鏄惧紡绌轰覆**锛坄EXTRA_ROOTS=`锛?| **涓€涓兘涓嶈拷鍔?*锛歚ALLOWED_ROOTS` 灏辨槸涓ユ牸鐨勬渶缁堢櫧鍚嶅崟锛岃繍缁村彲鐢ㄥ畠閿佹鍙叡浜寖鍥?|
| 璁句簡鍊?| 鍐掑彿鍒嗛殧杩藉姞锛屾敮鎸佸崟灞傞€氶厤锛堝 `EXTRA_ROOTS=/nas/mnt:/mnt/usb-*`锛?|

銆屾坊鍔犲叡浜€嶅脊绐楅噷鍙互閫愬眰娴忚锛堜綅缃細瀛樺偍姹?/ 澶栨帴瀛樺偍锛夛紝涔熷彲浠ョ洿鎺?*鎵嬪～缁濆璺緞**
锛堝 `/nas/mnt/usb/涓嬭浇`锛夛紱鏈嶅姟绔収鏃ф牎楠岀粷瀵硅矾寰勩€佹牴鐧藉悕鍗曘€乣..` 涓庣鍙烽摼鎺ョ殑鐪熷疄璺緞銆?
## 鍥涖€佹帴鍙ｄ笌椤甸潰

鎻掍欢鏈嶅姟鐩戝惉 `127.0.0.1:18190`锛岄〉闈㈢粡 nginx 璧?`/plugin/<鐢ㄦ埛>/netneighbor/`銆?
椤甸潰鍙湁涓夊潡锛?*缃戠粶鍙戠幇寮€鍏?+ 涓绘満鍚?*銆?*鍏变韩鐩綍**銆?*鐩綍娴忚鍣ㄥ脊绐?*銆?鍏变韩鐩綍鍒嗕笂涓嬩袱鍧楋細涓婇儴鏄€屾坊鍔犲叡浜€嶆寜閽紙鎵撳紑鐩綍娴忚鍣ㄩ€夌洰褰曪級锛屼笅閮ㄦ槸**宸插叡浜殑鐩綍**
鍒楄〃锛堟瘡琛岋細鏄剧ず鍚?+ 鍏变韩鍚?`<璐﹀彿>_nb_<搴忓彿>` + 瀹屾暣璺緞 + 琛屽熬 `-` 鍒犻櫎鎸夐挳锛屽垹鍓嶄簩娆＄‘璁わ紱
瀹樻柟 app 寤虹殑鍏变韩鐓ф棫鏄剧ず锛屼絾 `-` 缃伆骞惰鏄庡師鍥狅級銆?
鐩綍娴忚鍣ㄤ笌 transmission 鎻掍欢鍚屽舰锛氶《閮?*浣嶇疆鍒囨崲**锛堝瓨鍌ㄦ睜 / 澶栨帴瀛樺偍锛夈€佸綋鍓?*瀹屾暣缁濆璺緞**銆?閫愬眰杩涘叆瀛愮洰褰曘€併€屼笂涓€绾с€嶃€併€岄€夋嫨姝ょ洰褰曘€嶏紱涔熷彲浠ョ洿鎺ユ墜濉粷瀵硅矾寰勶紙`/nas/mnt` 涓嬬殑鐩綍锛?濡?`/nas/mnt/usb`锛夈€傛嫈鐩樻椂璇ヤ綅缃樉绀恒€屾湭鎺ュ叆銆嶅苟绂佹杩涘叆銆?
| 鏂规硶 | 璺緞 | 璇存槑 |
| --- | --- | --- |
| GET | `/healthz` | `{"ok": true, "version": "0.1.1"}` |
| GET | `/api/status` | 璁剧疆锛坄discoveryEnabled`锛夈€佸彧璇昏韩浠?`identity`锛坄systemName`/`netbiosName`/`matched`/`address`/`hint`/`warning`锛夈€佸彂鐜版湇鍔＄姸鎬併€佽处鍙蜂笌鍏变韩鍒楄〃锛涘伐浣滅粍銆乆Addrs銆佸畼鏂?wsdd 鐘舵€併€佹渶杩戝彇鍏冩暟鎹椂闂翠粛鍦ㄨ繑鍥為噷锛堜究浜庢帓鏌ワ級锛岄〉闈笉鏄剧ず |
| GET | `/api/log` | 鏈€杩戞棩蹇?|
| GET | `/api/browse?account=fw867&root=0&path=鐓х墖` | 鐩綍娴忚鍣細`root` 鏄綅缃簭鍙凤紙0 = 瀛樺偍姹?= 璐﹀彿鏁版嵁鏍癸紝鍏朵綑 = `EXTRA_ROOTS`锛岄粯璁?`/nas/mnt` 鏄剧ず涓恒€屽鎺ュ瓨鍌ㄣ€嶏級锛宍path` 鏄?*鐩稿璇ヤ綅缃?*鐨勮矾寰勶紙绌?= 浣嶇疆鏍圭洰褰曪級銆傝繑鍥?`locations`锛坄{index,label,path,exists}`锛夈€乣path`/`absolute`锛堝綋鍓嶇洰褰曪級涓?`items`锛堜竴灞傚瓙鐩綍锛歚{name,path,absolute,shared}`锛岃烦杩囬殣钘忕洰褰曚笌绗﹀彿閾炬帴锛夈€傝秺鐣岋紙`..`/缁濆璺緞锛夈€佷綅缃棤鏁堛€佺洰褰曚笉瀛樺湪閮借繑鍥?400 + 涓枃鍘熷洜 |
| GET | `/api/dirs?account=fw867` | 鑰佹帴鍙ｏ紙淇濈暀琛屼负涓庣粨鏋勶級锛氳处鍙锋暟鎹牴鐩綍涓嬬殑瀛愮洰褰曪紱姣忛」甯?`shared`銆乣shareName`/`display` 涓?`deletable`锛屽彟鏈?`locations` 鍒嗙粍锛坄{label, root, available, dirs}`锛?|
| POST | `/api/discovery` | `{"enabled": true\|false}`锛氬紑 = 鎺ョ瀹樻柟 wsdd + 璧峰洖搴斿櫒锛涘叧 = 鍙?Bye + 杩樺師瀹樻柟 wsdd锛堝箓绛夛級 |
| POST | `/api/hostname` | **宸插簾寮?*锛氫富鏈哄悕鏀规垚鍙灞曠ず锛堟湇鍔＄鑷姩鑾峰彇锛夛紝杩欓噷涓€寰嬭繑鍥?400銆屼富鏈哄悕涓嶅彲淇敼銆嶏紝涓嶅啓浠讳綍閰嶇疆 |
| POST | `/api/hostname/restore` | 銆屾仮澶嶄负绯荤粺涓绘満鍚嶃€嶏細鍐?`option name` = 绯荤粺涓绘満鍚?鈫?`smb_mgr.sh init_config` 鈫?`systemctl restart smb nmb wsdd` 鈫?鍥炶 `smb.conf` 鐨?`netbios name` 鏍￠獙锛岃繑鍥?`{name, netbiosName, verified, restartReturncode, units, errors, output}` |
| POST | `/api/share/add` | `{"account","path","sharePoint"}`锛堝崟涓紝鐩綍娴忚鍣ㄦ彁浜ょ殑灏辨槸**缁濆璺緞**锛涘吋瀹规棫鐗堬級鎴?`{"account","paths":[...]}`锛堝閫夛紱鎵€鏈?`add_dir` 涔嬪悗鍙窇涓€娆?`init_config` + reload锛?|
| POST | `/api/share/delete` | 鍒犻櫎鎻掍欢鑷缓鐨勫叡浜細`{"shareName": "<璐﹀彿>_nb_<搴忓彿>"}`锛堝崟涓紝杩斿洖缁撴瀯鍚屾棫鐗堬紱椤甸潰涓婅灏?`-` 鐢ㄧ殑灏辨槸瀹冿級鎴?`{"shareNames": [...]}`锛堟壒閲忥紱鎵€鏈?`del_dir` 涔嬪悗鍙窇涓€娆?`init_config` + reload锛岃繑鍥?`removed/verified/errors/initReturncode/reloadReturncode`锛夈€?*鍚彈淇濇姢鍏变韩锛坄public` 绛夛級鏃舵暣鎵规嫆缁濄€佷竴鏉″懡浠ら兘涓嶈窇** |
| POST | `/api/detect/restart` | 閲嶅惎鍥炲簲鍣ㄥ苟閲嶅彂 Hello锛堟帓鏌ョ敤锛岄〉闈笂娌℃湁鎸夐挳锛?|

鍐欐搷浣滈渶瑕佷細璇濅护鐗?+ `X-CSRF-Token`锛堜笌浠撳簱鍏跺畠鎻掍欢涓€鑷达級銆傞〉闈㈢敱**鎻掍欢鏈嶅姟**鍙戝嚭
锛坣ginx 閲屾槸 `proxy_pass`锛屼笉鏄潤鎬?`alias`锛夛細`index.html` 閲岀殑 `__SESSION_TOKEN__` /
`__CSRF_TOKEN__` / `__PLUGIN_VERSION__` 鍗犱綅绗﹀彧鏈夋湇鍔＄浼氭浛鎹紝鏀规垚 alias 浼氳椤甸潰鎷垮埌
绌轰护鐗屻€佹墍鏈夋帴鍙?401銆?
鍓嶇涓嶅啓娴忚鍣ㄨ嚜鍔ㄥ寲娴嬭瘯锛屼絾鏈変袱鏉￠潤鎬佸厹搴曪細`web/app.js` 閲屽紩鐢ㄧ殑姣忎釜 id 閮借鍦?`web/index.html` 閲屽瓨鍦紙`scripts/check_web_ids.py`锛夛紝鍏抽敭鍏冪礌/浜や簰閽╁瓙鐢?`tests/test_web.py` 鏂█锛?
```bash
python3 scripts/check_web_ids.py
node --check web/app.js
python3 -m unittest tests.test_web -v
```

### SMB 鍚嶏細鍙灞曠ず锛堜笉鍐嶆彁渚涙敼鍚嶏級

鐪熸満韪╄繃鐨勫潙锛氭彃浠跺師鏉ヨ鐢ㄦ埛鏀圭殑鏄?**Samba 鐨?NetBIOS 鍚?*锛堝啓 `/etc/config/samba` 鐨?`option name` 鈫?`netbios name`锛夛紝浣?Windows 11 鏄潬 **WSD/mDNS/DNS**锛堜篃灏辨槸**绯荤粺涓绘満鍚?*锛?鍙戠幇璁惧鐨勶紱涓よ€呬笉涓€鑷存椂鐢ㄦ埛鏀规垚浠讳綍鏂板悕瀛楅兘杩炰笉涓婏紝鑰屼笖 `netbios name` 鍙?`reload smb nmb`
涔熶笉鐢熸晥锛堝繀椤?`restart`锛夈€?*鎵€浠ユ彃浠朵笉鍐嶆彁渚涙敼鍚嶈兘鍔?*锛氶〉闈㈠彧璇诲睍绀轰竴琛?SMB 鍚嶏紝
绯荤粺涓绘満鍚嶅彧鍦ㄦ湇鍔＄浣滀负銆屼袱鍚嶆槸鍚︿竴鑷淬€嶇殑鍐呴儴鍒ゆ嵁锛堥〉闈笉灞曠ず锛夈€?
| 椤甸潰鏄剧ず | 鍙栧€兼潵婧愶紙鍏ㄩ儴鏈嶅姟绔嚜鍔ㄨ幏鍙栵級 |
| --- | --- |
| SMB 鍚?| `/var/etc/smb.conf` 鐨?`netbios name` 鈫?`/etc/config/samba` 鐨?`option name` |
| 璁块棶鎻愮ず | SMB 鍚?+ 灞€鍩熺綉 IP 鐜版嫾锛歚Windows 閲岀敤 \\<SMB鍚? 鎴?\\<灞€鍩熺綉IP> 璁块棶` |
| 锛堝唴閮ㄥ垽鎹級绯荤粺涓绘満鍚?| `uci -q get system.@system[0].hostname` 鈫?绯荤粺璋冪敤 `hostname` 鈫?`/etc/config/samba` 鐨?`option name` 鈫?鍐呯疆榛樿鍊?|

IP 鐢?`ip route get 223.5.5.5` 鐨?`src` 鎺㈡祴锛堟嬁涓嶅埌灏遍€€鍒?`wsd.lan_address()` 鐨勯粯璁よ矾鐢辨帰娴嬶級锛?IP 鍙栦笉鍒版椂鎻愮ず鍙粰 `\\<SMB鍚?` 閭ｄ竴鍗婏紝鍚嶅瓧涔熻涓嶅埌灏变笉鏄剧ず鎻愮ず銆?
涓や釜鍚嶅瓧涓嶄竴鑷达紙澶у皬鍐欎笉鏁忔劅锛夋椂锛岄〉闈㈠湪 SMB 鍚嶄笅鏂规樉绀?*榛勮壊璀﹀憡**骞剁粰鍑恒€屾仮澶嶄负绯荤粺涓绘満鍚嶃€嶆寜閽細

```
鍐?/etc/config/samba 鐨?option name = 绯荤粺涓绘満鍚?  鈫?smb_mgr.sh init_config
  鈫?systemctl restart smb nmb wsdd     # 涓嶆槸 reload锛歯etbios name 鍙?reload 涓嶇敓鏁?  鈫?鍥炶 smb.conf 鐨?netbios name 鏍￠獙锛堢粨鏋滀笌澶辫触鍘熷洜閮芥彁绀哄湪椤甸潰涓婏級
```

椤甸潰娌℃湁浠讳綍涓绘満鍚嶈緭鍏ユ锛沗POST /api/hostname` 涓€寰嬭繑鍥?400銆屼富鏈哄悕涓嶅彲淇敼銆嶏紝
`settings.json` 閲屾棫鐗堟湰鐨?`hostname` 閿篃涓嶅啀璇诲啓锛堝崌绾у悗绗竴娆′繚瀛樹細鑷劧涓㈡帀瀹冿級銆?
銆岀綉缁滃彂鐜般€嶅紑鍏崇殑鐘舵€佸悓鏍疯惤鐩樺湪 `settings.json`锛氬叧闂悗鏈嶅姟閲嶅惎**涓嶄細**鑷姩鎺ョ锛?鐩村埌鐢ㄦ埛鍦ㄩ〉闈笂閲嶆柊鎵撳紑銆?
## 浜斻€佸畨瑁呭悗鐨勮涓?
- 鏈嶅姟锛歚xiaomi-netneighbor.service`锛坄/data/plugin/netneighbor/current/server.py`锛夛紝
  鏁版嵁鐩綍 `/data/plugin/netneighbor/data`锛堣缃惤鐩樺湪 `data/settings.json`锛夛紱
- 銆岀綉缁滃彂鐜般€嶅紑鍏抽粯璁ゆ墦寮€锛堜繚鎸佸崌绾у墠鐨勮涓猴級锛屽叧鎺夊悗鍋滅敤/閲嶅惎閮戒笉浼氬啀鎺ョ锛?  鍋滅敤/鍗歌浇鏃朵竴瀹氳繕鍘熷畼鏂?wsdd锛?- 闇€瑕?root锛氳鎵ц `systemctl`銆佸啓 UCI銆佽皟鐢?`smb_mgr.sh`銆傚崟鍏冮噷鐢?  `ProtectSystem=full` + `ReadWritePaths=/etc/config /etc/systemd/system /data/plugin/netneighbor`
  鎶婂彲鍐欒寖鍥存敹鍒版渶灏忋€?
## 鍏€佹祴璇?
```bash
cd projects/xiaomi-netneighbor-plugin
python3 -m unittest discover -s tests -v
```

涓嶄緷璧栫涓夋柟鍖咃紝涔熶笉浼氱湡鐨勫幓鍔ㄧ郴缁燂細`subprocess`銆乣systemctl`銆乣smb_mgr.sh` 涓?`WsdResponder` 鍏ㄩ儴鍙敞鍏ワ紝娴嬭瘯鐢ㄥ亣 runner 鏂█鍛戒护搴忓垪銆?
## 涓冦€佺湡鏈哄疄娴嬭褰曪紙2026-09-28锛孯P05 / 192.168.1.8锛?
- **鍙戠幇鏈嶅姟**锛氭彃浠跺惎鍔ㄥ悗瀹樻柟 wsdd 琚仠鎺夛紙drop-in 鐢熸晥锛夛紝3702 鐢辨彃浠惰繘绋嬫寔鏈夛紱
  Windows 璧勬簮绠＄悊鍣ㄣ€岀綉缁溿€嶉噷鍑虹幇 `SMARTSTORAGE`锛屾彃浠舵棩蹇楄兘鐪嬪埌
  `鏀跺埌 Probe` 鈫?`wsd http: POST 鈥?action=Get 鈫?200`锛宍metadataPosts` 閫掑銆?  鍙?`Bye` 鍚庡浘鏍囨秷澶便€佸啀 `Hello` 鍙堝洖鏉ャ€?- **`systemctl restart` 鐨勭珵鎬?*锛氬仠鏈嶆椂 `ExecStopPost --restore-wsdd` 浼氭妸瀹樻柟 wsdd
  鎷夊洖鏉ワ紝鏂板疄渚嬬殑鎺ョ浼氬啀鎶婂畠鍋滄帀锛堝仠瀹岃嚜妫€ 3702銆佸繀瑕佹椂閲嶈瘯涓€娆★級锛屽叏绋嬫棤闇€浜哄伐骞查
  鈥斺€?杩欐潯姝ｆ槸瀹炴祴韪╁嚭鏉ョ殑锛氭渶鍒?`stop` 閭ｆ婕忎簡 `systemctl` 鍓嶇紑锛堟棩蹇楅噷鏄?  銆屾壘涓嶅埌鍛戒护 stop銆嶏級锛屽畼鏂硅繘绋嬩竴鐩村崰鐫€ 3702锛屽洖搴斿櫒姘歌繙璧蜂笉鏉ャ€?- **鍏变韩绠＄悊**锛歚POST /api/share/add` 缁欒处鍙?`fw867` 鍔?  `/home/u3943892/pool0/data/鎴戠殑鐓х墖` 鈫?鐢熸垚 `fw867_nb_1`锛?  杩斿洖閲?`verified / inConfig / smbActive` 鍧囦负 `true`锛?  `net view \\192.168.1.8` 鍚屼竴璐﹀彿涓嬪嚭鐜颁袱涓叡浜紝
  `\\192.168.1.8\鐓х墖-3943892` 鑳界洿鎺ュ垪鍑烘枃浠讹紱
  `POST /api/share/delete` 鍒犳帀鍚庨厤缃仮澶嶅師鏍凤紝鑰屽垹瀹樻柟閭ｆ潯浼氳鎷掔粷
  锛堛€岀敱绯荤粺鎴栧皬绫?App 绠＄悊銆嶏級銆?- 杩欏彴 NAS **娌℃湁 `pkill`**锛屽彧鏈?`killall`锛屾墍浠ュ厹搴曟竻鐞嗘槸鎸?  `killall`/`pkill` 鐨勯『搴忔帰娴嬪疄闄呭瓨鍦ㄧ殑閭ｄ釜銆?
## 鍏€佸凡鐭ラ檺鍒?
- 鍙鐞?*鎻掍欢鑷缓**鐨勫叡浜紱app 閲岄偅涓?`<璐﹀彿>_<id>` 浠嶇敱瀹樻柟鐣岄潰绠°€?- 鎻掍欢鏂板鐨勭洰褰曚笉浼氬嚭鐜板湪鍏变韩 app 鐨勭晫闈笂锛堝畠鍙鑷繁鍐欑殑閭ｄ竴鏉★級锛屼絾 SMB 閲屾甯稿彲鐢ㄣ€?- **鍒氭柊澧炵殑鍏变韩鍦?Windows 閲屽彲鑳借绛変竴涓嬫墠鑳芥墦寮€**锛歚smb.conf` 鏄嵆鏃剁敓鏁堢殑
  锛坄testparm` 绔嬪埢鑳界湅鍒伴偅涓€娈碉級锛屼絾 SMB 瀹㈡埛绔細缂撳瓨鍏变韩鍒楄〃涓庡凡寤虹珛鐨勪細璇濓紝鍒氱偣瀹?  銆岀‘瀹氥€嶅氨鍙屽嚮鏂板叡浜彲鑳芥姤銆屾壘涓嶅埌缃戠粶璺緞 / 鏃犳硶璁块棶銆嶏紱閲嶅紑涓€娆¤祫婧愮鐞嗗櫒绐楀彛銆?  鎴栨柇寮€閲嶈繛锛坄net use \\<NAS-IP> /delete`锛変箣鍚庡氨姝ｅ父浜嗐€?026-09-28 鐢ㄦ埛瀹炴祴纭杩囪繖涓幇璞★紝
  椤甸潰娣诲姞鎴愬姛鍚庝篃浼氭彁绀轰竴鍙ャ€學indows 閲屽彲鑳借閲嶅紑璧勬簮绠＄悊鍣ㄦ墠鐪嬪緱鍒般€嶃€?- **`list dirs` 涓庡叡浜涓嶅悓姝?*锛歚sambauser` 鐨?`list dirs` 瀛樼殑鏄洰褰曡矾寰勶紝
  `sambashare` 鎵嶆槸鍏变韩娈点€傝嫢鏌愮洰褰曞彧鍦?`list dirs` 閲岋紙渚嬪鍏变韩娈佃鍒犳帀锛夛紝椤甸潰浼氭妸瀹?  鏄剧ず鎴愩€屾湭鍏变韩銆嶏紝姝ゆ椂鍙互鐢ㄦ彃浠堕噸鏂板叡浜畠锛涘弽杩囨潵锛屾彃浠朵笉浼氬幓鍔?`list dirs`锛?  鎵€浠ュ畼鏂圭晫闈笂鐪嬩笉鍒版彃浠舵柊澧炵殑鐩綍銆?- **绗﹀彿閾炬帴鍙尅寰椾綇 `/nas` 涓嬬殑**锛歚validate_share_path` 浼氭妸绗﹀彿閾炬帴瑙ｆ瀽鍚庣殑鐪熷疄璺緞
  涔熸嬁鍘绘瘮鐧藉悕鍗曪紝浣?`SYMLINK_ROOT` 鏄?`/home`锛岃€岀櫧鍚嶅崟涓诲姏涔熸槸 `/home/<uXXXX>/pool0/data`
  鈥斺€擿/home` 涓嬬殑杞摼鎬庝箞瑙ｆ瀽閮借惤鍦?`/home/` 寮€澶达紝绛変簬涓嶈闃层€傝繖鏄€屾彃浠跺彧鏈嶅姟璁惧鎵€鏈夎€?  鑷繁鐨勮处鍙枫€嶈繖涓€鍓嶆彁涓嬬殑鍙栬垗銆?- `..` 鐨勬樉寮忔鏌ュ湪 `posixpath.normpath` **涔嬪悗**锛岀粷瀵硅矾寰勯噷鐨?`..` 浼氳鍏堟姌鍙狅紝
  鐪熸鍏滀綇瓒婄晫鐨勬槸鐧藉悕鍗曢偅涓€鍏筹紙鎶?`/nas/pool0/../etc` 鎶樺彔鎴?`/nas/etc` 鍐嶆瘮锛夈€?- 鍏佽鏍圭洰褰曠殑鎺ㄥ锛坄derive_allowed_roots`锛夌敤 `Path(root)` 鎷艰矾寰勶紝鍙湪 Linux 涓婃纭?  鈥斺€旀彃浠舵湰鏉ュ氨鍙窇鍦?NAS 涓婏紱鍗曟祴閲屾妸涓や釜銆屾寜骞冲彴鎷艰矾寰勩€嶇殑灏忓伐鍏锋墦妗╂垚 POSIX 褰㈡€併€?- 鍚屼竴鍙?NAS 涓婂悓鏃惰 dpanel 涓?disksleep 浼氬洜涓虹鍙ｉ兘鍐?18160 鑰屽啿绐侊紙涓庢湰鎻掍欢鏃犲叧锛?  鏈彃浠剁敤 18190锛夈€?