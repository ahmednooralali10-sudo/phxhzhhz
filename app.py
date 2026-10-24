import os
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

import sys, time, json, ssl, socket, threading, asyncio, random, struct, uuid
from datetime import datetime
from threading import Thread
from typing import Optional, Dict, List

import urllib3
import httpx
import urllib.request
import importlib.util
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google_play_scraper import app as play_scraper
from protobuf_decoder.protobuf_decoder import Parser

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import uvicorn

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================
#  PROTOBUF DEFINITIONS
# ============================================================
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
            raise RuntimeError("Downloaded file too small — probably 404")
        with open(PROTO_LOCAL, "wb") as f:
            f.write(data)
        print("[+] Proto.py downloaded successfully.")
    except Exception as e:
        print(f"[!] Failed to download Proto.py: {e}")
        sys.exit(1)

_ensure_proto_file()

_spec = importlib.util.spec_from_file_location("Proto", PROTO_LOCAL)
_proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_proto)

MajorLogin_pb = _proto.MajorLogin_pb
MajorLoginRes_pb = _proto.MajorLoginRes_pb
GetLoginDataRes_pb = _proto.GetLoginDataRes_pb

# ============================================================
#  CONFIG
# ============================================================
C = "\033[96m"; G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
RS = "\033[0m"; BOLD = "\033[1m"; M = "\033[95m"

ACCOUNTS_TXT_FILE = "Ariyan.txt"
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
TOKEN_CACHE_TTL = 1200
KEEP_ALIVE_INTERVAL = 5.0
MAX_LOGIN_ATTEMPTS = 2
NUM_WORKERS = 3
REQ_PER_BOT_PER_SEC = 8
V_INTERVAL = 1.0

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
    verify=False, timeout=15.0,
    limits=httpx.Limits(max_connections=1000, max_keepalive_connections=500)
)

# ============================================================
#  GLOBAL STATE
# ============================================================
TARGET_UID: Optional[str] = None
online_accounts = []
online_lock = threading.Lock()

_loop: Optional[asyncio.AbstractEventLoop] = None
_login_task: Optional[asyncio.Task] = None
_dispatch_task: Optional[asyncio.Task] = None
_running = False
_state_lock = threading.Lock()

_avatar_cursor = 0
_avatar_lock = threading.Lock()

def get_next_avatar():
    global _avatar_cursor
    with _avatar_lock:
        if _avatar_cursor >= len(AVATAR_POOL):
            _avatar_cursor = 0
        a = AVATAR_POOL[_avatar_cursor]
        _avatar_cursor += 1
    return a

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
        B = N & 0x7F; N >>= 7
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

async def yasser_SendInv_packet(bot_uid, target_uid, avatar_id, K, V):
    fields = {
        1: 33,
        2: {
            1: int(target_uid), 2: "ME", 3: 1, 4: 1,
            6: "yyt", 7: 330, 8: 1000, 9: 100,
            10: "DZ", 12: 1, 13: int(target_uid), 16: 1,
            17: {2: 159, 4: "y[WW", 6: 11, 8: "1.118.1", 9: 3, 10: 1},
            18: 306, 19: 18, 24: 902000306,
            26: {}, 27: {1: 11, 2: int(bot_uid), 3: 999},
            28: {}, 31: {1: 1, 2: 32768}, 32: 32768,
            34: {
                1: int(bot_uid), 2: 8,
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
    nd = _generate_new_device_mm(); devices[acc_key] = nd
    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f: json.dump(devices, f, indent=4)
    except: pass
    return nd

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

    def _q(srv):
        s = None
        try:
            tid = random.randint(1000, 65535)
            hdr = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
            qn = b"".join(bytes([len(p)]) + p.encode('ascii') for p in hostname.split('.')) + b"\x00"
            pkt = hdr + qn + struct.pack(">HH", 1, 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.2)
            s.sendto(pkt, (srv, 53))
            resp, _ = s.recvfrom(1024)
            if len(resp) >= 12:
                anc = struct.unpack(">H", resp[6:8])[0]
                if anc > 0:
                    off = 12 + len(qn) + 4
                    for _ in range(anc):
                        if off >= len(resp): break
                        if (resp[off] & 0xC0) == 0xC0: off += 2
                        else:
                            while off < len(resp) and resp[off] != 0: off += 1 + resp[off]
                            off += 1
                        if off + 10 > len(resp): break
                        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[off:off+10]); off += 10
                        if rtype == 1 and rdlen == 4 and off + 4 <= len(resp):
                            return socket.inet_ntoa(resp[off:off+4])
                        off += rdlen
        except: pass
        finally:
            if s:
                try: s.close()
                except: pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _q, CLOUDFLARE_PRIMARY_DNS)
    if not ip: ip = await loop.run_in_executor(None, _q, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            infos = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if infos: ip = infos[0][4][0]
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
            c = f.read().strip()
        return _json_deserializer(json.loads(c)) if c else {}
    except: return {}

def _save_token_cache(cache):
    try:
        tmp = TOKEN_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=_json_serializer)
        os.replace(tmp, TOKEN_CACHE_FILE)
    except: pass

def cache_get(uid):
    c = _load_token_cache(); e = c.get(str(uid))
    if not e: return None
    if time.time() - e.get("cached_at", 0) > TOKEN_CACHE_TTL:
        cache_invalidate(uid); return None
    if str(e.get("account_id", "")).isdigit(): e["account_id"] = int(e["account_id"])
    if not isinstance(e.get("login_payload_data"), (bytes, bytearray)):
        cache_invalidate(uid); return None
    return e

def cache_set(uid, data):
    c = _load_token_cache(); e = dict(data); e["cached_at"] = time.time()
    c[str(uid)] = e; _save_token_cache(c)

def cache_invalidate(uid):
    c = _load_token_cache()
    if str(uid) in c:
        del c[str(uid)]; _save_token_cache(c)

# ============================================================
#  LOGIN PIPELINE
# ============================================================
async def aes_encrypt_mm(payload, key, iv):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(payload, AES.block_size))

def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict): return default
    if key in d:
        v = d[key].get('data'); return v if v is not None else default
    if str(key) in d:
        v = d[str(key)].get('data'); return v if v is not None else default
    return default

async def parse_results_mm(parsed_results):
    out = {}
    for r in parsed_results:
        fd = {"wire_type": r.wire_type}
        if r.wire_type == "varint": fd["data"] = r.data
        elif r.wire_type == "string": fd["data"] = r.data
        elif r.wire_type == "bytes": fd["data"] = r.data
        elif r.wire_type == "length_delimited":
            if hasattr(r.data, "results"): fd["data"] = await parse_results_mm(r.data.results)
            elif isinstance(r.data, list): fd["data"] = await parse_results_mm(r.data)
            else: fd["data"] = str(r.data)
        out[str(r.field)] = fd
    return out

async def get_playstore_version_mm():
    loop = asyncio.get_event_loop()
    try:
        r = await loop.run_in_executor(None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id'))
        return r.get("version")
    except: return "1.132.6"

async def version_config_mm():
    try:
        av = await get_playstore_version_mm() or "1.132.6"
        url = (f"https://version.ggwhitehawk.com/live/ver.php?version={av}"
               "&lang=hi&device=android&channel=android&appstore=googleplay&region=BD"
               "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")
        resp = await asyncio.get_event_loop().run_in_executor(None, lambda: _client_mm.get(url, timeout=8.0))
        if resp.status_code == 200:
            d = resp.json()
            s, r, l = d.get("server_url"), d.get("remote_version"), d.get("latest_release_version")
            if s and r and l: return (l, r, s)
    except: pass
    return ("OB55", "1.132.6", "https://clientbp.ggpolarbear.com/live/")

async def get_access_token_mm(uid, password):
    url = "https://100067.connect.garena.com/api/v2/oauth/guest/token:grant"
    hdrs = {"Host": "100067.connect.garena.com",
            "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 13;en;IN;)",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate, br", "Connection": "close"}
    payload = {"client_id": 100067,
               "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
               "client_type": 2, "password": str(password).strip(),
               "response_type": "token", "uid": int(str(uid).strip())}
    for attempt in range(2):
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _client_mm.post(url, headers=hdrs, json=payload))
            if r.status_code == 200:
                j = r.json(); inner = j.get("data", j)
                o, a, p = inner.get("open_id"), inner.get("access_token"), inner.get("platform", 4)
                if o and a: return o, a, p
            elif r.status_code == 429:
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
        print(f"{R}❌ build_majorlogin error: {e}{RS}")
        return None

async def send_majorlogin_mm(data, release_version, server_url):
    try:
        url = f"{server_url}MajorLogin"
        h = headers_mm.copy(); h["ReleaseVersion"] = release_version
        r = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _client_mm.post(url, headers=h, data=data))
        if r.status_code != 200: return None
        c = r.content
        if len(c) < 40: return None
        payload = c[64:] if len(c) > 64 else c
        proto = MajorLoginRes_pb()
        try:
            proto.ParseFromString(payload)
            if proto.token: return proto
        except: pass
        for off in range(min(128, len(payload))):
            try:
                cand = MajorLoginRes_pb(); cand.ParseFromString(payload[off:])
                if cand.token: return cand
            except: pass
        try:
            proto = MajorLoginRes_pb(); proto.ParseFromString(c); return proto
        except: return None
    except: return None

async def send_getlogin_mm(data, base_url, token, release_version):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        h = headers_mm.copy(); h["ReleaseVersion"] = release_version
        h['Authorization'] = f"Bearer {token}"; h['Host'] = "clientbp.ppmainecoonghj.com"
        r = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _client_mm.post(url, headers=h, data=data))
        if r.status_code != 200: return None
        c = r.content; proto = GetLoginDataRes_pb(); ok = False
        try:
            proto.ParseFromString(c)
            if proto.functional_addrs or proto.informational_addrs: ok = True
        except: pass
        if not ok:
            for off in range(min(128, len(c))):
                try:
                    cand = GetLoginDataRes_pb(); cand.ParseFromString(c[off:])
                    if cand.functional_addrs or cand.informational_addrs:
                        proto = cand; break
                except: pass
        dr = {}
        try:
            dr = await parse_results_mm(Parser().parse(c.hex()))
        except: pass
        return proto, dr
    except: return None

async def build_tcp_startup_packet_mm(account_id, token, server_time, key, iv, typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"; ts_hex = f"{int(server_time):08x}"
    enc = (await aes_encrypt_mm(token.encode(), key, iv)).hex()
    ln = f"{len(enc)//2:08x}"
    if typ == 'OnLine':
        return f"7219{uid_hex}{ts_hex}00000000{ln}{enc}"
    return f"8119{uid_hex}{ts_hex}{ln}{enc}"

async def send_keep_alive_mm():
    return bytes.fromhex("0219")

async def process_account_uid_pass_mm(uid, password):
    cached = cache_get(uid)
    if cached: return cached
    try:
        v = await version_config_mm()
        if not v: return None
        rel, cver, srv = v
        t = await get_access_token_mm(uid, password)
        if not t: return None
        oid, at, plat = t
        dev = get_device_for_account_mm(uid)
        payload = await build_majorlogin_payload_mm(oid, at, plat, cver, dev)
        if not payload: return None
        mlr = await send_majorlogin_mm(payload, rel, srv)
        if not mlr: return None
        glr = await send_getlogin_mm(payload, mlr.server_url, mlr.token, rel)
        if not glr: return None
        rp, dr = glr
        acc_id = str(mlr.account_id)
        data = {
            'account_id': mlr.account_id,
            'nickname': rp.nickname or get_proto_field(dr, 4, f"Player_{acc_id}"),
            'region': mlr.noti_region or get_proto_field(dr, 3, "BD"),
            'level': int(get_proto_field(dr, 6, 1)),
            'exp': int(get_proto_field(dr, 7, 0)),
            'likes': int(get_proto_field(dr, 8, 0)),
            'open_id': oid, 'access_token': at, 'platform': str(plat),
            'token': mlr.token, 'server_time': mlr.kts,
            'aes_ak': mlr.ak, 'iv_i': mlr.aiv,
            'functional_addrs': rp.functional_addrs or get_proto_field(dr, 14),
            'informational_addrs': rp.informational_addrs or get_proto_field(dr, 32),
            'release_version': rel, 'client_version': cver,
            'server_url': mlr.server_url, 'login_payload_data': payload,
            'auth_type': 'guest', 'auth_uid': uid, 'auth_password': password
        }
        cache_set(uid, data)
        return data
    except Exception as e:
        print(f"{R}❌ process_account error: {e}{RS}")
        return None

# ============================================================
#  ONLINE WORKER
# ============================================================
async def run_account_online_worker_mm(account_data):
    acc_id = str(account_data['account_id'])
    nickname = account_data.get('nickname', 'Player')
    level = account_data.get('level', 1)
    fa = account_data.get('functional_addrs')
    key = account_data.get('aes_ak'); iv = account_data.get('iv_i')
    if not fa: return
    ip, port = fa.split(":")
    while True:
        writer = None; reader = None
        try:
            tcp = await build_tcp_startup_packet_mm(
                account_data['account_id'], account_data['token'],
                account_data['server_time'], account_data['aes_ak'],
                account_data['iv_i'], typ='OnLine')
            rip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(rip, int(port))
            raw = writer.get_extra_info('socket')
            if raw: optimize_tcp_socket(raw)
            writer.write(bytes.fromhex(tcp)); await writer.drain()
            ka = await send_keep_alive_mm()
            if ka and not writer.is_closing():
                writer.write(ka); await writer.drain()
            print(f"{G}✅ ONLINE{RS} [{acc_id}] | {Y}{nickname}{RS} | Lvl {C}{level}{RS}")
            with online_lock:
                online_accounts.append({'uid': acc_id, 'writer': writer, 'key': key, 'iv': iv})
            while True:
                writer.write(ka); await writer.drain()
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=KEEP_ALIVE_INTERVAL)
                    if data == b'': raise ConnectionError("Server closed")
                except asyncio.TimeoutError: pass
        except asyncio.CancelledError:
            with online_lock:
                online_accounts[:] = [a for a in online_accounts if a['uid'] != acc_id]
            await safe_close_writer(writer)
            raise
        except Exception as e:
            print(f"{Y}⚠️ Connection lost {acc_id} ({e}). Reconnecting 5s...{RS}")
            with online_lock:
                online_accounts[:] = [a for a in online_accounts if a['uid'] != acc_id]
            await safe_close_writer(writer)
            await asyncio.sleep(5)

# ============================================================
#  ACCOUNTS LOADER
# ============================================================
def load_accounts_from_ariyan_txt():
    accounts = []
    if os.path.exists(ACCOUNTS_TXT_FILE):
        try:
            with open(ACCOUNTS_TXT_FILE, "r", encoding="utf-8") as f:
                for line in f.readlines():
                    l = line.strip()
                    if not l or l.startswith("#") or l.startswith("//"): continue
                    parts = None
                    for delim in [":", "|", ",", " "]:
                        if delim in l:
                            p = [x.strip() for x in l.split(delim, 1)]
                            if len(p) == 2 and p[0] and p[1]:
                                parts = p; break
                    if parts:
                        u, p = parts
                        if u.isdigit(): accounts.append({"uid": u, "password": p})
        except Exception as e:
            print(f"{R}❌ Error reading {ACCOUNTS_TXT_FILE}: {e}{RS}")
    return accounts

def load_accounts():
    # 1) env var
    raw = os.environ.get("ACCOUNTS_DATA", "").strip()
    if raw:
        accs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            parts = None
            for delim in [":", "|", ",", " "]:
                if delim in line:
                    p = [x.strip() for x in line.split(delim, 1)]
                    if len(p) == 2 and p[0] and p[1]: parts = p; break
            if parts:
                u, p = parts
                if u.isdigit(): accs.append({"uid": u, "password": p})
        if accs: return accs
    # 2) local file
    return load_accounts_from_ariyan_txt()

# ============================================================
#  WORKER QUEUE
# ============================================================
async def worker_login_queue(worker_id, queue):
    while True:
        try:
            acc_info = await queue.get()
        except asyncio.CancelledError:
            break
        if acc_info is None:
            queue.task_done(); break
        idx = acc_info['idx']; total = acc_info['total']
        uid = acc_info['uid']; pwd = acc_info['password']
        ad = None
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            print(f"{C}[W{worker_id}] [{idx}/{total}] Logging in: {uid} (Attempt {attempt}/{MAX_LOGIN_ATTEMPTS})...{RS}")
            try:
                ad = await process_account_uid_pass_mm(uid, pwd)
                if ad: break
                if attempt < MAX_LOGIN_ATTEMPTS: await asyncio.sleep(1.5)
            except:
                if attempt < MAX_LOGIN_ATTEMPTS: await asyncio.sleep(1.5)
        if not ad:
            print(f"{R}[W{worker_id}] [{idx}/{total}] Skipped UID: {uid}{RS}")
            queue.task_done(); continue
        asyncio.create_task(run_account_online_worker_mm(ad))
        await asyncio.sleep(0.8)
        queue.task_done()

async def mm_main_loop():
    accounts = load_accounts()
    if not accounts:
        print(f"{R}❌ No accounts found!{RS}")
        return
    total = len(accounts)
    print(f"{C}🔁 {total} accounts | {NUM_WORKERS} workers{RS}")
    queue = asyncio.Queue()
    for idx, acc in enumerate(accounts, start=1):
        await queue.put({'idx': idx, 'total': total,
                         'uid': acc['uid'], 'password': acc['password']})
    for _ in range(NUM_WORKERS):
        await queue.put(None)
    workers = []
    for w in range(1, NUM_WORKERS + 1):
        workers.append(asyncio.create_task(worker_login_queue(w, queue)))
    await asyncio.gather(*workers, return_exceptions=True)
    print(f"{G}✅ All {total} accounts processed. Keep-alive loops running.{RS}")
    while True:
        await asyncio.sleep(60)

# ============================================================
#  V-BADGE DISPATCHER
# ============================================================
async def bot_blast_task(acc, cycle_no):
    sent = 0
    for i in range(REQ_PER_BOT_PER_SEC):
        if TARGET_UID is None: break
        try:
            av = get_next_avatar()
            pkt = await yasser_SendInv_packet(acc['uid'], TARGET_UID, av, acc['key'], acc['iv'])
            if pkt and not acc['writer'].is_closing():
                acc['writer'].write(pkt); await acc['writer'].drain()
                sent += 1
                print(f"{G}  ✓ [{acc['uid']}] #{cycle_no}.{i+1} → {TARGET_UID} (avatar: {Y}{av}{G}){RS}")
        except Exception as e:
            print(f"{R}❌ [{acc['uid']}] vbadge error: {e}{RS}")
    return sent

async def vbadge_dispatcher_loop():
    global TARGET_UID
    print(f"{G}▶ V-BADGE dispatcher waiting for online accounts...{RS}")
    while True:
        with online_lock:
            if online_accounts: break
        await asyncio.sleep(0.5)
    print(f"{G}▶ V-BADGE dispatcher STARTED → target {TARGET_UID}{RS}")
    cycle_no = 0
    while True:
        try:
            with online_lock:
                snap = list(online_accounts)
            if not snap:
                await asyncio.sleep(1.0); continue
            cycle_no += 1; total_sent = 0; total_bots = len(snap)
            for bi, acc in enumerate(snap, start=1):
                if TARGET_UID is None: break
                print(f"{C}🤖 Bot {bi}/{total_bots} [{acc['uid']}] blasting {REQ_PER_BOT_PER_SEC} req...{RS}")
                total_sent += await bot_blast_task(acc, f"{cycle_no}-{bi}")
            print(f"{C}── cycle {cycle_no}: {total_sent} req across {total_bots} bots ──{RS}")
            await asyncio.sleep(V_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"{R}❌ dispatcher error: {e}{RS}")
            await asyncio.sleep(2)

# ============================================================
#  SERVICE CONTROL
# ============================================================
def _run_service_thread():
    global _loop, _login_task, _dispatch_task
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    async def _runner():
        global _login_task, _dispatch_task
        _login_task = asyncio.create_task(mm_main_loop())
        await asyncio.sleep(2)
        _dispatch_task = asyncio.create_task(vbadge_dispatcher_loop())
        try:
            await asyncio.gather(_login_task, _dispatch_task, return_exceptions=True)
        except asyncio.CancelledError: pass

    try:
        _loop.run_until_complete(_runner())
    except Exception as e:
        print(f"service thread error: {e}")
    finally:
        try: _loop.close()
        except: pass

def start_service(target_uid: str):
    global TARGET_UID, _service_thread, _running
    with _state_lock:
        if _running:
            TARGET_UID = target_uid
            return False, "Service already running — target updated."
        TARGET_UID = target_uid
        _service_thread = Thread(target=_run_service_thread, daemon=True)
        _service_thread.start()
        _running = True
        return True, f"Service started → target {target_uid}"

def stop_service():
    global _running, _loop, _login_task, _dispatch_task, TARGET_UID
    with _state_lock:
        if not _running: return False, "Service is not running."
        TARGET_UID = None
        _running = False
        try:
            if _loop and not _loop.is_closed():
                def _cancel():
                    for t in (_login_task, _dispatch_task):
                        if t and not t.done(): t.cancel()
                _loop.call_soon_threadsafe(_cancel)
        except Exception as e:
            print(f"stop error: {e}")
        with online_lock:
            for a in online_accounts:
                try:
                    if a.get('writer') and not a['writer'].is_closing():
                        a['writer'].close()
                except: pass
            online_accounts.clear()
        return True, "Service stopped."

# ============================================================
#  FASTAPI APP
# ============================================================
app = FastAPI(title="Ariyan V-Badge API", version="1.0")

@app.get("/")
def root():
    return {
        "name": "Ariyan V-Badge API",
        "endpoints": {
            "start": "/start?uid=TARGET_UID",
            "stop": "/stop?uid=TARGET_UID",
            "status": "/status",
            "health": "/health",
        },
    }

@app.get("/health")
def health():
    return {"ok": True, "running": _running, "online_bots": len(online_accounts)}

@app.get("/start")
def api_start(uid: str = Query(..., description="Target UID")):
    if not uid.isdigit():
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "uid must be numeric"})
    accs = load_accounts()
    if not accs:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "No accounts configured. Set ACCOUNTS_DATA env or add Ariyan.txt.",
        })
    started, msg = start_service(uid)
    return {
        "ok": True, "started": started, "message": msg,
        "target_uid": TARGET_UID, "accounts_loaded": len(accs),
    }

@app.get("/stop")
def api_stop(uid: Optional[str] = Query(None)):
    stopped, msg = stop_service()
    return {"ok": True, "stopped": stopped, "message": msg, "requested_uid": uid}

@app.get("/status")
def api_status():
    with online_lock:
        bots = [{"uid": a["uid"]} for a in online_accounts]
    return {
        "running": _running,
        "target_uid": TARGET_UID,
        "online_bots_count": len(bots),
        "online_bots": bots,
        "config": {
            "req_per_bot_per_sec": REQ_PER_BOT_PER_SEC,
            "interval": V_INTERVAL,
            "avatar_pool_size": len(AVATAR_POOL),
            "num_workers": NUM_WORKERS,
        },
    }

# ============================================================
#  MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, log_level="info")
