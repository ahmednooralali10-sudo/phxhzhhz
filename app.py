import os
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
import sys, time, json, ssl, socket, threading, asyncio, random, struct, uuid
from datetime import datetime
from threading import Thread

import urllib3
import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google_play_scraper import app as play_scraper
from protobuf_decoder.protobuf_decoder import Parser

from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================
#  PROTOBUF DEFINITIONS
# ============================================================
import urllib.request

PROTO_URL = "https://raw.githubusercontent.com/mdrajababof2378866-hue/Lojik/refs/heads/main/Proto.py"
PROTO_LOCAL = "Proto.py"

def _ensure_proto_file():
    if os.path.exists(PROTO_LOCAL) and os.path.getsize(PROTO_LOCAL) > 500:
        return
    print("[*] Downloading Proto.py from GitHub...")
    try:
        req = urllib.request.Request(PROTO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        if len(data) < 500:
            raise RuntimeError("Downloaded file too small")
        with open(PROTO_LOCAL, "wb") as f:
            f.write(data)
        print("[+] Proto.py downloaded successfully.")
    except Exception as e:
        print(f"[!] Failed to download Proto.py: {e}")
        sys.exit(1)

_ensure_proto_file()

import importlib.util
_spec = importlib.util.spec_from_file_location("Proto", PROTO_LOCAL)
_proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_proto)

MajorLogin_pb = _proto.MajorLogin_pb
MajorLoginRes_pb = _proto.MajorLoginRes_pb
GetLoginDataRes_pb = _proto.GetLoginDataRes_pb

# ============================================================
#  CONFIG (تم تسريع العمليات هنا)
# ============================================================
ACCOUNTS_TXT_FILE = "Ariyan.txt"
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
TOKEN_CACHE_TTL = 1200
KEEP_ALIVE_INTERVAL = 5.0
MAX_LOGIN_ATTEMPTS = 3
NUM_WORKERS = 20  # زدنا عدد العمال عشان تسجيل الدخول يكون أسرع
REQ_PER_BOT_PER_SEC = 10  # زدنا سرعة الضرب
V_INTERVAL = 0.05 # قللنا وقت الانتظار بين الضربات

AVATAR_POOL = [
    902055033, 902055004, 902055031, 902055001, 902055032,
    902055006, 902055006, 902055005, 902055003, 902055001,
    902054017, 902054007, 902052027, 902051038, 902027016,
    902027017, 902028018, 902034023, 902000067,
]

AES_KEY_MM = b'Yg&tc%DEuh6%Zc^8'
AES_IV_MM = b'6oyZDr22E3ychjM%'

headers_mm = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55'
}

_client_mm = httpx.Client(
    verify=False,
    timeout=15.0,
    limits=httpx.Limits(max_connections=1000, max_keepalive_connections=500)
)

online_accounts = []
online_lock = threading.Lock()
active_tasks = {} # {uid: asyncio.Event}

_avatar_cursor = 0
_avatar_lock = threading.Lock()

def get_next_avatar():
    global _avatar_cursor
    with _avatar_lock:
        if _avatar_cursor >= len(AVATAR_POOL):
            _avatar_cursor = 0
        avatar = AVATAR_POOL[_avatar_cursor]
        _avatar_cursor += 1
    return avatar

# ============================================================
#  ASYNC PACKET HELPERS
# ============================================================
async def EnC_PacKeT_async(HeX, K, V):
    return AES.new(K, AES.MODE_CBC, V).encrypt(pad(bytes.fromhex(HeX), 16)).hex()

async def _dec_hex_async(H):
    F = str(hex(H))[2:]
    return ('0' + F) if len(F) == 1 else F

async def _enc_varint_async(N):
    H = []
    while True:
        B = N & 0x7F
        N >>= 7
        if N: B |= 0x80
        H.append(B)
        if not N: break
    return bytes(H)

async def _create_variant_async(fn, val):
    return await _enc_varint_async((fn << 3) | 0) + await _enc_varint_async(val)

async def _create_length_async(fn, val):
    ev = val.encode() if isinstance(val, str) else val
    return await _enc_varint_async((fn << 3) | 2) + await _enc_varint_async(len(ev)) + ev

async def create_proto_async(fields):
    packet = bytearray()
    for field, value in fields.items():
        if isinstance(value, dict):
            nested = await create_proto_async(value)
            packet.extend(await _create_length_async(field, nested))
        elif isinstance(value, int):
            packet.extend(await _create_variant_async(field, value))
        elif isinstance(value, (str, bytes)):
            packet.extend(await _create_length_async(field, value))
    return bytes(packet)

async def generate_packet_async(hex_payload, ptype, K, V):
    enc = await EnC_PacKeT_async(hex_payload, K, V)
    length_hex = await _dec_hex_async(len(enc) // 2)
    if len(length_hex) == 2: header = ptype + "000000"
    elif len(length_hex) == 3: header = ptype + "00000"
    elif len(length_hex) == 4: header = ptype + "0000"
    elif len(length_hex) == 5: header = ptype + "000"
    else: header = ptype + "0000"
    return bytes.fromhex(header + length_hex + enc)

# ============================================================
#  V-BADGE PACKET
# ============================================================
async def yasser_SendInv_packet(bot_uid, target_uid, avatar_id, K, V):
    fields = {
        1: 33,
        2: {
            1: int(target_uid), 2: "ME", 3: 1, 4: 1,
            6: "yyt", 7: 330, 8: 1000, 9: 100,
            10: "DZ", 12: 1, 13: int(target_uid),
            16: 1,
            17: {2: 159, 4: "y[WW", 6: 11, 8: "1.118.1", 9: 3, 10: 1},
            18: 306, 19: 18, 24: 902000306,
            26: {}, 27: {1: 11, 2: int(bot_uid), 3: 999},
            28: {},
            31: {1: 1, 2: 32768},
            32: 32768,
            34: {
                1: int(bot_uid),
                2: 8,
                3: "\u0010\u0015\b\n\u000b\u0013\f\u000f\u0011\u0004\u0007\u0002\u0003\r\u000e\u0012\u0001\u0005\u0006",
                4: int(avatar_id),
            }
        }
    }
    proto = await create_proto_async(fields)
    return await generate_packet_async(proto.hex(), '0515', K, V)

# ============================================================
#  DEVICE HELPERS
# ============================================================
def _generate_new_device_mm():
    device_list = [
        ("Xiaomi", "M2006C3LII", "PowerVR Rogue GE8320", "Android OS 10 / API-29 (QP1A.190711.020/V12.0.26.0.QCDINXM)"),
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
    ]
    brand, model, gpu, os_ver = random.choice(device_list)
    return {"unique_device_id": f"Google|{uuid.uuid4()}", "brand": brand, "model": model,
            "gpu_renderer": gpu, "system_software": os_ver,
            "screen_width": random.choice([1080, 1600]), "screen_height": random.choice([2400, 720]),
            "screen_dpi": str(random.randint(300, 420)), "memory": random.randint(2800, 6500),
            "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
            "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}"}

def get_device_for_account_mm(account_identifier):
    devices = {}
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                devices = json.load(f)
                if not isinstance(devices, dict): devices = {}
        except: devices = {}
    acc_key = str(account_identifier).strip()
    if acc_key in devices: return devices[acc_key]
    new_device = _generate_new_device_mm()
    devices[acc_key] = new_device
    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f: json.dump(devices, f, indent=4)
    except: pass
    return new_device

# ============================================================
#  DNS / SOCKET
# ============================================================
CLOUDFLARE_PRIMARY_DNS = "1.1.1.1"
CLOUDFLARE_SECONDARY_DNS = "1.0.0.1"
_DNS_CACHE = {}
_DNS_CACHE_TTL = 300.0

async def resolve_host_cloudflare(hostname):
    if not hostname: return hostname
    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts): return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp: return ip

    def _query_cloudflare(server_ip):
        s = None
        try:
            tx_id = random.randint(1000, 65535)
            header = struct.pack(">HHHHHH", tx_id, 0x0100, 1, 0, 0, 0)
            qname = b"".join(bytes([len(part)]) + part.encode('ascii') for part in hostname.split('.')) + b"\x00"
            query_pkt = header + qname + struct.pack(">HH", 1, 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.2)
            s.sendto(query_pkt, (server_ip, 53))
            resp, _ = s.recvfrom(1024)
            if len(resp) >= 12:
                ancount = struct.unpack(">H", resp[6:8])[0]
                if ancount > 0:
                    offset = 12 + len(qname) + 4
                    for _ in range(ancount):
                        if offset >= len(resp): break
                        if (resp[offset] & 0xC0) == 0xC0: offset += 2
                        else:
                            while offset < len(resp) and resp[offset] != 0: offset += 1 + resp[offset]
                            offset += 1
                        if offset + 10 > len(resp): break
                        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[offset:offset+10]); offset += 10
                        if rtype == 1 and rdlen == 4 and offset + 4 <= len(resp): return socket.inet_ntoa(resp[offset:offset+4])
                        offset += rdlen
        except: pass
        finally:
            if s:
                try: s.close()
                except: pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_PRIMARY_DNS)
    if not ip: ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            ip_info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if ip_info: ip = ip_info[0][4][0]
        except: ip = hostname
    if ip: _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
    return ip or hostname

def optimize_tcp_socket(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
    except: pass

async def safe_close_writer(writer):
    if not writer: return
    try:
        if not writer.is_closing(): writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=1.5)
    except: pass

# ============================================================
#  TOKEN CACHE
# ============================================================
def _json_serializer(obj):
    if isinstance(obj, (bytes, bytearray)): return {"__bytes_hex__": bytes(obj).hex()}
    raise TypeError(f"Type {type(obj)} not serializable")

def _json_deserializer(obj):
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj and len(obj) == 1:
            try: return bytes.fromhex(obj["__bytes_hex__"])
            except: return b""
        return {k: _json_deserializer(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_json_deserializer(x) for x in obj]
    return obj

def _load_token_cache():
    if not os.path.exists(TOKEN_CACHE_FILE): return {}
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content: return {}
        return _json_deserializer(json.loads(content))
    except: return {}

def _save_token_cache(cache):
    try:
        tmp = TOKEN_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(cache, f, indent=2, default=_json_serializer)
        os.replace(tmp, TOKEN_CACHE_FILE)
    except: pass

def cache_get(uid):
    cache = _load_token_cache()
    entry = cache.get(str(uid))
    if not entry: return None
    if time.time() - entry.get("cached_at", 0) > TOKEN_CACHE_TTL:
        cache_invalidate(uid); return None
    if str(entry.get("account_id", "")).isdigit(): entry["account_id"] = int(entry["account_id"])
    if not isinstance(entry.get("login_payload_data"), (bytes, bytearray)):
        cache_invalidate(uid); return None
    return entry

def cache_set(uid, account_data):
    cache = _load_token_cache()
    entry = dict(account_data); entry["cached_at"] = time.time()
    cache[str(uid)] = entry; _save_token_cache(cache)

def cache_invalidate(uid):
    cache = _load_token_cache()
    if str(uid) in cache:
        del cache[str(uid)]; _save_token_cache(cache)

# ============================================================
#  LOGIN PIPELINE
# ============================================================
async def aes_encrypt_mm(payload, key, iv):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(payload, AES.block_size))

def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict): return default
    if key in d:
        val = d[key].get('data')
        return val if val is not None else default
    if str(key) in d:
        val = d[str(key)].get('data')
        return val if val is not None else default
    return default

async def parse_results_mm(parsed_results):
    result_dict = {}
    for result in parsed_results:
        field_data = {"wire_type": result.wire_type}
        if result.wire_type == "varint": field_data["data"] = result.data
        elif result.wire_type == "string": field_data["data"] = result.data
        elif result.wire_type == "bytes": field_data["data"] = result.data
        elif result.wire_type == "length_delimited":
            if hasattr(result.data, "results"): field_data["data"] = await parse_results_mm(result.data.results)
            elif isinstance(result.data, list): field_data["data"] = await parse_results_mm(result.data)
            else: field_data["data"] = str(result.data)
        result_dict[str(result.field)] = field_data
    return result_dict

async def get_playstore_version_mm():
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id'))
        return result.get("version")
    except: return "1.132.6"

async def version_config_mm():
    try:
        app_version = await get_playstore_version_mm() or "1.132.6"
        api_url = (f"https://version.ggwhitehawk.com/live/ver.php?version={app_version}"
                   "&lang=hi&device=android&channel=android&appstore=googleplay&region=BD"
                   "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")
        response = await asyncio.get_running_loop().run_in_executor(None, lambda: _client_mm.get(api_url, timeout=8.0))
        if response.status_code == 200:
            data = response.json()
            s_url = data.get("server_url"); r_ver = data.get("remote_version"); l_rel = data.get("latest_release_version")
            if s_url and r_ver and l_rel: return (l_rel, r_ver, s_url)
    except: pass
    return ("OB55", "1.132.6", "https://clientbp.ggpolarbear.com/live/")

async def get_access_token_mm(uid, password):
    url = "https://100067.connect.garena.com/api/v2/oauth/guest/token:grant"
    hdrs = {"Host": "100067.connect.garena.com", "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 13;en;IN;)",
            "Content-Type": "application/json", "Accept-Encoding": "gzip, deflate, br", "Connection": "close"}
    payload = {"client_id": 100067, "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
               "client_type": 2, "password": str(password).strip(), "response_type": "token", "uid": int(str(uid).strip())}
    for attempt in range(2):
        try:
            response = await asyncio.get_running_loop().run_in_executor(None, lambda: _client_mm.post(url, headers=hdrs, json=payload))
            if response.status_code == 200:
                res_data = response.json(); inner = res_data.get("data", res_data)
                open_id = inner.get("open_id"); access_token = inner.get("access_token"); platform = inner.get("platform", 4)
                if open_id and access_token: return open_id, access_token, platform
            elif response.status_code == 429:
                await asyncio.sleep(1.0); continue
        except: pass
        await asyncio.sleep(0.3)
    return None

async def build_majorlogin_payload_mm(open_id, access_token, platform, client_version, device_info):
    try:
        ml = MajorLogin_pb()
        ml.open_id = open_id; ml.access_token = access_token; ml.client_version = client_version
        ml.event_time = str(datetime.now())[:-7]; ml.game_name = "free fire"; ml.platform_id = 1
        ml.system_software = "Android OS 10 / API-29 (QP1A.190711.020/V12.0.26.0.QCDINXM)"
        ml.system_hardware = "Handheld"; ml.telecom_operator = "Ncell"; ml.network_type = "WIFI"
        ml.screen_width = 1600; ml.screen_height = 720; ml.screen_dpi = "320"
        ml.processor_details = "ARMv7 VFPv3 NEON | 2001 | 8"; ml.memory = 3790
        ml.gpu_renderer = "PowerVR Rogue GE8320"; ml.gpu_version = "OpenGL ES 3.2 build 1.11@5425693"
        ml.unique_device_id = "Google|00000000-0000-0000-0000-000000000000"
        ml.client_ip = "111.119.38.133"; ml.language = "en"; ml.open_id_type = "4"; ml.device_type = "Handheld"
        ml.device_model = "Xiaomi M2006C3LII"; ml.country_code = "BD"; ml.platform_sdk_id = 1
        ml.network_operator_a = "Ncell"; ml.network_type_a = "WIFI"
        ml.client_using_version = "1ac4b80ecf0478a44203bf8fac6120f5"
        ml.external_storage_total = 53041; ml.external_storage_available = 7291
        ml.internal_storage_total = 2176; ml.game_disk_storage_available = 7395
        ml.game_disk_storage_total = 53041; ml.external_sdcard_avail_storage = 7395; ml.external_sdcard_total_storage = 53041
        ml.field_70 = 4; ml.login_by = 2
        ml.library_path = "/data/app/com.dts.freefireth-yAPXAhp2RyIlrtNAM0VzKQ==/lib/arm"
        ml.reg_avatar = 1
        ml.library_token = "066a589fa3f5658377634fe7b1d88556|/data/app/com.dts.freefireth-yAPXAhp2RyIlrtNAM0VzKQ==/base.apk"
        ml.channel_type = 6; ml.cpu_type = 1; ml.cpu_architecture = "32"; ml.client_version_code = "2019121227"
        ml.field_85 = 3; ml.graphics_api = "OpenGLES2"; ml.supported_astc_bitset = 3071
        ml.login_open_id_type = 4; ml.loading_time = 9329; ml.release_channel = "3rd_party"
        ml.extra_info = "KqsHT3r+fXQIu/dyZrEa8fJBhbJ5uqDES7YsAUfu+Mck9A+Bly6lFfYk7Q7Nj68pqI8I3g4Oz3gLxWef6Eh/jKyzHug="
        ml.android_engine_init_flag = 111207
        ml.field_96 = json.dumps({"cur_rate": None, "support_etc2": False}, separators=(',', ':'))
        ml.if_push = 1; ml.origin_platform_type = "4"; ml.primary_platform_type = "4"
        ml.field_102 = bytes.fromhex("42 54 4c 10 53 0e 5b 04 30"); ml.field_104 = 47591; ml.field_105 = 1
        ml.field_106 = "https://dl-bs.ggpolarbear.com/live/ABHotUpdates/|https://core-bs.ggpolarbear.com/live/ABHotUpdates/|1c2462939e53942fc995400436a3dc7b"
        ml.field_107 = "c8e41b7a93f02d56e1a94c7b8203f5d1"
        return await aes_encrypt_mm(ml.SerializeToString(), AES_KEY_MM, AES_IV_MM)
    except Exception as e:
        return None

async def send_majorlogin_mm(data, release_version, server_url):
    try:
        url = f"{server_url}MajorLogin"
        req_headers = headers_mm.copy(); req_headers["ReleaseVersion"] = release_version
        response = await asyncio.get_running_loop().run_in_executor(None, lambda: _client_mm.post(url, headers=req_headers, data=data))
        if response.status_code != 200: return None
        response_content = response.content
        if len(response_content) < 40: return None
        proto_payload = response_content[64:] if len(response_content) > 64 else response_content
        res_proto = MajorLoginRes_pb()
        try:
            res_proto.ParseFromString(proto_payload)
            if res_proto.token: return res_proto
        except: pass
        for offset in range(min(128, len(proto_payload))):
            try:
                candidate = MajorLoginRes_pb(); candidate.ParseFromString(proto_payload[offset:])
                if candidate.token: return candidate
            except: pass
        try:
            res_proto = MajorLoginRes_pb(); res_proto.ParseFromString(response_content); return res_proto
        except: return None
    except: return None

async def send_getlogin_mm(data, base_url, token, release_version):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        req_headers = headers_mm.copy(); req_headers["ReleaseVersion"] = release_version
        req_headers['Authorization'] = f"Bearer {token}"; req_headers['Host'] = "clientbp.ppmainecoonghj.com"
        response = await asyncio.get_running_loop().run_in_executor(None, lambda: _client_mm.post(url, headers=req_headers, data=data))
        if response.status_code != 200: return None
        response_content = response.content
        res_proto = GetLoginDataRes_pb(); parsed_successfully = False
        try:
            res_proto.ParseFromString(response_content)
            if res_proto.functional_addrs or res_proto.informational_addrs: parsed_successfully = True
        except: pass
        if not parsed_successfully:
            for offset in range(min(128, len(response_content))):
                try:
                    candidate = GetLoginDataRes_pb(); candidate.ParseFromString(response_content[offset:])
                    if candidate.functional_addrs or candidate.informational_addrs: res_proto = candidate; break
                except: pass
        dict_res = {}
        try:
            parsed = Parser().parse(response_content.hex()); dict_res = await parse_results_mm(parsed)
        except: pass
        return res_proto, dict_res
    except: return None

async def build_tcp_startup_packet_mm(account_id, token, server_time, key, iv, typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"; timestamp_hex = f"{int(server_time):08x}"
    encode_token = token.encode()
    encrypted_packet = (await aes_encrypt_mm(encode_token, key, iv)).hex()
    encrypted_packet_length = f"{len(encrypted_packet) // 2:08x}"
    if typ == 'OnLine':
        return f"7219{uid_hex}{timestamp_hex}00000000{encrypted_packet_length}{encrypted_packet}"
    else:
        return f"8119{uid_hex}{timestamp_hex}{encrypted_packet_length}{encrypted_packet}"

async def send_keep_alive_mm():
    return bytes.fromhex("0219")

async def process_account_uid_pass_mm(uid, password):
    cached = cache_get(uid)
    if cached: return cached
    try:
        verconfig_res = await version_config_mm()
        if not verconfig_res: return None
        release_version, client_version, server_url = verconfig_res
        tokengrant_res = await get_access_token_mm(uid, password)
        if not tokengrant_res: return None
        open_id, access_token, platform = tokengrant_res
        device_info = get_device_for_account_mm(uid)
        login_payload_data = await build_majorlogin_payload_mm(open_id, access_token, platform, client_version, device_info)
        if not login_payload_data: return None
        majorlogin_res = await send_majorlogin_mm(login_payload_data, release_version, server_url)
        if not majorlogin_res: return None
        getlogin_res = await send_getlogin_mm(login_payload_data, majorlogin_res.server_url, majorlogin_res.token, release_version)
        if not getlogin_res: return None
        res_proto, dict_res = getlogin_res
        acc_id = str(majorlogin_res.account_id)
        level = int(get_proto_field(dict_res, 6, 1)); exp = int(get_proto_field(dict_res, 7, 0)); likes = int(get_proto_field(dict_res, 8, 0))
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{acc_id}")
        region = majorlogin_res.noti_region or get_proto_field(dict_res, 3, "BD")
        account_data = {
            'account_id': majorlogin_res.account_id, 'nickname': nickname, 'region': region,
            'level': level, 'exp': exp, 'likes': likes, 'open_id': open_id, 'access_token': access_token,
            'platform': str(platform), 'token': majorlogin_res.token, 'server_time': majorlogin_res.kts,
            'aes_ak': majorlogin_res.ak, 'iv_i': majorlogin_res.aiv,
            'functional_addrs': res_proto.functional_addrs or get_proto_field(dict_res, 14),
            'informational_addrs': res_proto.informational_addrs or get_proto_field(dict_res, 32),
            'release_version': release_version, 'client_version': client_version,
            'server_url': majorlogin_res.server_url, 'login_payload_data': login_payload_data,
            'auth_type': 'guest', 'auth_uid': uid, 'auth_password': password
        }
        cache_set(uid, account_data)
        return account_data
    except Exception as e:
        return None

# ============================================================
#  ONLINE WORKER
# ============================================================
async def run_account_online_worker_mm(account_data):
    acc_id = str(account_data['account_id'])
    nickname = account_data.get('nickname', 'Player')
    level = account_data.get('level', 1)
    func_addrs = account_data.get('functional_addrs')
    key = account_data.get('aes_ak')
    iv = account_data.get('iv_i')
    if not func_addrs: return
    ip, port = func_addrs.split(":")
    while True:
        writer = None; reader = None
        try:
            tcp_startup = await build_tcp_startup_packet_mm(
                account_data['account_id'], account_data['token'], account_data['server_time'],
                account_data['aes_ak'], account_data['iv_i'], typ='OnLine')
            resolved_ip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(resolved_ip, int(port))
            raw_sock = writer.get_extra_info('socket')
            if raw_sock: optimize_tcp_socket(raw_sock)
            writer.write(bytes.fromhex(tcp_startup)); await writer.drain()
            init_ka = await send_keep_alive_mm()
            if init_ka and not writer.is_closing(): writer.write(init_ka); await writer.drain()
            
            with online_lock:
                online_accounts.append({'uid': acc_id, 'writer': writer, 'key': key, 'iv': iv})
            ka_bytes = await send_keep_alive_mm()
            while True:
                writer.write(ka_bytes); await writer.drain()
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=KEEP_ALIVE_INTERVAL)
                    if data == b'': raise ConnectionError("Server closed")
                except asyncio.TimeoutError: pass
        except asyncio.CancelledError:
            try:
                with online_lock:
                    online_accounts[:] = [a for a in online_accounts if a['uid'] != acc_id]
            except: pass
            await safe_close_writer(writer)
            raise
        except Exception:
            try:
                with online_lock:
                    online_accounts[:] = [a for a in online_accounts if a['uid'] != acc_id]
            except: pass
            await safe_close_writer(writer)
            await asyncio.sleep(5)

# ============================================================
#  ACCOUNTS LOADER & LOGIN WORKERS
# ============================================================
def load_accounts_from_ariyan_txt():
    accounts = []
    if os.path.exists(ACCOUNTS_TXT_FILE):
        try:
            with open(ACCOUNTS_TXT_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                if content.strip().startswith("["):
                    data = json.loads(content)
                    for item in data:
                        if "uid" in item and "password" in item:
                            accounts.append({"uid": str(item["uid"]), "password": item["password"]})
                else:
                    for line in content.splitlines():
                        l = line.strip()
                        if not l or l.startswith("#") or l.startswith("//"): continue
                        parts = None
                        for delim in [":", "|", ",", " "]:
                            if delim in l:
                                p = [x.strip() for x in l.split(delim, 1)]
                                if len(p) == 2 and p[0] and p[1]: parts = p; break
                        if parts:
                            u, p = parts
                            if u.isdigit(): accounts.append({"uid": u, "password": p})
        except Exception as e: print(f"Error reading {ACCOUNTS_TXT_FILE}: {e}")
    return accounts

async def worker_login_queue(worker_id, queue):
    while True:
        try: acc_info = await queue.get()
        except asyncio.CancelledError: break
        if acc_info is None: queue.task_done(); break
        uid = acc_info['uid']; pwd = acc_info['password']

        account_data = None
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            account_data = await process_account_uid_pass_mm(uid, pwd)
            if account_data: break
            else: await asyncio.sleep(1.0)

        if account_data:
            asyncio.create_task(run_account_online_worker_mm(account_data))
        queue.task_done()
        await asyncio.sleep(0.1) # تسريع العملية

async def mm_main_loop():
    accounts = load_accounts_from_ariyan_txt()
    if not accounts: return
    total = len(accounts)
    queue = asyncio.Queue()
    for acc in accounts:
        await queue.put({'uid': acc['uid'], 'password': acc['password']})
    for _ in range(NUM_WORKERS):
        await queue.put(None)

    workers = []
    for w in range(1, NUM_WORKERS + 1):
        t = asyncio.create_task(worker_login_queue(w, queue))
        workers.append(t)

    await asyncio.gather(*workers, return_exceptions=True)

# ============================================================
#  V-BADGE API SPAMMER
# ============================================================
async def spam_worker_for_uid(target_uid, stop_event):
    while not stop_event.is_set():
        with online_lock:
            snapshot = list(online_accounts)

        if not snapshot:
            await asyncio.sleep(1)
            continue

        for acc in snapshot:
            if stop_event.is_set(): break
            try:
                avatar_id = get_next_avatar()
                pkt = await yasser_SendInv_packet(acc['uid'], target_uid, avatar_id, acc['key'], acc['iv'])
                if pkt and not acc['writer'].is_closing():
                    acc['writer'].write(pkt)
                    await acc['writer'].drain()
            except Exception:
                pass
        
        await asyncio.sleep(V_INTERVAL)

# ============================================================
#  FLASK API ROUTES
# ============================================================
app = Flask(__name__)
loop = asyncio.new_event_loop()

def start_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

# تشغيل الـ Loop في الخلفية عشان يشتغل الـ API والـ Async مع بعض
threading.Thread(target=start_loop, daemon=True).start()

# تشغيل تسجيل الدخول للحسابات في الخلفية بمجرد تشغيل السيرفر
loop.call_soon_threadsafe(lambda: asyncio.create_task(mm_main_loop()))

@app.route('/spam', methods=['GET'])
def spam():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Invalid UID"}), 400
    
    if uid in active_tasks:
        return jsonify({"status": "already_spamming", "uid": uid}), 200

    stop_event = asyncio.Event()
    active_tasks[uid] = stop_event
    
    # جدولة المهمة داخل الـ Loop
    loop.call_soon_threadsafe(lambda: asyncio.create_task(spam_worker_for_uid(uid, stop_event)))
    
    return jsonify({"status": "started", "uid": uid, "online_bots": len(online_accounts)}), 200

@app.route('/stop', methods=['GET'])
def stop():
    uid = request.args.get('uid')
    if not uid:
        return jsonify({"error": "Missing UID"}), 400
    
    if uid in active_tasks:
        stop_event = active_tasks[uid]
        loop.call_soon_threadsafe(stop_event.set)
        del active_tasks[uid]
        return jsonify({"status": "stopped", "uid": uid}), 200
    else:
        return jsonify({"error": "No active spam for this UID"}), 404

@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "online_bots": len(online_accounts),
        "active_targets": list(active_tasks.keys())
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
