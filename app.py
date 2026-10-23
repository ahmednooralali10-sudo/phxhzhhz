# MINISTER VISIT API SRC 
# POWERED BY : @minister_69
# CHANNEL : @minister_6T9
from flask import Flask, jsonify, request
import aiohttp
import asyncio
import json
from byte import encrypt_api, Encrypt_ID
from visit_count_pb2 import Info

app = Flask(__name__)

def load_tokens(server_name):
    """
    সার্ভার নাম অনুযায়ী JSON ফাইল থেকে টোকেন লোড করে।
    রিটার্ন করে: (tokens_list, count)
    """
    try:
        if server_name == "IND":
            path = "token_ind.json"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            path = "token_br.json"
        else:
            path = "token_bd.json"

        with open(path, "r") as f:
            data = json.load(f)

        tokens = [item["token"] for item in data if "token" in item and item["token"] not in ["", "N/A"]]
        return tokens, len(tokens)
    except Exception as e:
        app.logger.error(f"❌ Token load error for {server_name}: {e}")
        return [], 0

def get_url(server_name):
    """
    সার্ভারের নাম অনুযায়ী সঠিক URL রিটার্ন করে।
    """
    if server_name == "IND":
        return "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        # BD, ME, PK, TH, VN, ID – সবগুলো এই URL ব্যবহার করবে
        return "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

def parse_protobuf_response(response_data):
    try:
        info = Info()
        info.ParseFromString(response_data)
        
        player_data = {
            "uid": info.AccountInfo.UID if info.AccountInfo.UID else 0,
            "nickname": info.AccountInfo.PlayerNickname if info.AccountInfo.PlayerNickname else "",
            "likes": info.AccountInfo.Likes if info.AccountInfo.Likes else 0,
            "region": info.AccountInfo.PlayerRegion if info.AccountInfo.PlayerRegion else "",
            "level": info.AccountInfo.Levels if info.AccountInfo.Levels else 0
        }
        return player_data
    except Exception as e:
        app.logger.error(f"❌ Protobuf parsing error: {e}")
        return None

async def visit(session, url, token, uid, data):
    headers = {
        "ReleaseVersion": "OB54",
        "X-GA": "v1 1",
        "Authorization": f"Bearer {token}",
        "Host": url.replace("https://", "").split("/")[0]
    }
    try:
        async with session.post(url, headers=headers, data=data, ssl=False) as resp:
            if resp.status == 200:
                response_data = await resp.read()
                return True, response_data
            else:
                return False, None
    except Exception as e:
        app.logger.error(f"❌ Visit error: {e}")
        return False, None

async def send_until_1000_success(tokens, uid, server_name, target_success=1000):
    url = get_url(server_name)
    connector = aiohttp.TCPConnector(limit=0)
    total_success = 0
    total_sent = 0
    first_success_response = None
    player_info = None

    async with aiohttp.ClientSession(connector=connector) as session:
        encrypted = encrypt_api("08" + Encrypt_ID(str(uid)) + "1801")
        data = bytes.fromhex(encrypted)

        while total_success < target_success:
            batch_size = min(target_success - total_success, 300)
            tasks = [
                asyncio.create_task(visit(session, url, tokens[(total_sent + i) % len(tokens)], uid, data))
                for i in range(batch_size)
            ]
            results = await asyncio.gather(*tasks)
            
            if first_success_response is None:
                for success, response in results:
                    if success and response is not None:
                        first_success_response = response
                        player_info = parse_protobuf_response(response)
                        break
            
            batch_success = sum(1 for r, _ in results if r)
            total_success += batch_success
            total_sent += batch_size

            print(f"Batch sent: {batch_size}, Success in batch: {batch_success}, Total success so far: {total_success}")

    return total_success, total_sent, player_info

@app.route('/visit', methods=['GET'])
def send_visits_query():
    uid = request.args.get('uid')
    server_name = request.args.get('server_name')
    key = request.args.get('key')

    # প্যারামিটার ভ্যালিডেশন
    if not uid or not server_name or not key:
        return jsonify({"error": "Missing parameters: uid, server_name, key"}), 400

    if key != "MNSTR":
        return jsonify({"error": "Invalid API key"}), 401

    try:
        uid_int = int(uid)
    except ValueError:
        return jsonify({"error": "uid must be an integer"}), 400

    server = server_name.upper()
    
    # টোকেন লোড করুন এবং কাউন্ট নিন
    tokens, token_count = load_tokens(server)

    if not tokens:
        return jsonify({
            "error": "❌ No valid tokens found for server",
            "available_accounts": 0
        }), 500

    target_success = 1000
    print(f"🚀 Sending visits to UID: {uid_int} using {token_count} tokens")
    print(f"Waiting for total {target_success} successful visits...")

    total_success, total_sent, player_info = asyncio.run(
        send_until_1000_success(tokens, uid_int, server, target_success)
    )

    if player_info:
        response = {
            "nickname": player_info.get("nickname", ""),
            "uid": player_info.get("uid", 0)
            "level": player_info.get("level", 0),
            "likes": player_info.get("likes", 0),
            "region": player_info.get("region", ""),
            "available_accounts": token_count,
            "total_success": total_success,
            "total_failed": target_success - total_success,
        }
        return jsonify(response), 200
    else:
        return jsonify({
            "error": "Could not decode player information",
            "available_accounts": token_count
        }), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
    
    
# MINISTER VISIT API SRC 
# POWERED BY : @minister_69
# CHANNEL : @minister_6T9