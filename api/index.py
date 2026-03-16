import os
import json
import html
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GIST_ID = os.getenv("GIST_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GIST_API = f"https://api.github.com/gists/{GIST_ID}"
GIST_FILENAME = "tg_notify_subscribers.json"


# -----------------------------
# 基本檢查
# -----------------------------
def env_ok():
    return all([BOT_TOKEN, BOT_USERNAME, GITHUB_TOKEN, GIST_ID, WEBHOOK_SECRET])


# -----------------------------
# Telegram API
# -----------------------------
def tg(method: str, payload: dict = None):
    url = f"{TELEGRAM_API}/{method}"
    resp = requests.post(url, json=payload or {}, timeout=20)
    try:
        return resp.json()
    except Exception:
        return {"ok": False, "status_code": resp.status_code, "text": resp.text}


def send_message(chat_id: int, text: str, reply_to_message_id: int = None, parse_mode: str = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg("sendMessage", payload)


def delete_message(chat_id: int, message_id: int):
    return tg("deleteMessage", {
        "chat_id": chat_id,
        "message_id": message_id
    })


def get_chat_member(chat_id: int, user_id: int):
    return tg("getChatMember", {"chat_id": chat_id, "user_id": user_id})


# -----------------------------
# Gist 儲存
# -----------------------------
def gist_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def load_db():
    resp = requests.get(GIST_API, headers=gist_headers(), timeout=20)
    resp.raise_for_status()
    data = resp.json()

    files = data.get("files", {})
    file_obj = files.get(GIST_FILENAME)

    if not file_obj:
        return {"groups": {}}

    content = file_obj.get("content", "").strip()
    if not content:
        return {"groups": {}}

    try:
        parsed = json.loads(content)
        if "groups" not in parsed:
            parsed["groups"] = {}
        return parsed
    except Exception:
        return {"groups": {}}


def save_db(db: dict):
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(db, ensure_ascii=False, indent=2)
            }
        }
    }
    resp = requests.patch(GIST_API, headers=gist_headers(), json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


# -----------------------------
# 工具函式
# -----------------------------
def is_private_chat(message: dict) -> bool:
    chat = message.get("chat", {})
    return chat.get("type") == "private"


def is_group_chat(message: dict) -> bool:
    chat = message.get("chat", {})
    return chat.get("type") in ("group", "supergroup")


def get_user_display_name(user: dict) -> str:
    first_name = user.get("first_name", "") or ""
    last_name = user.get("last_name", "") or ""
    full = (first_name + " " + last_name).strip()
    if full:
        return full
    if user.get("username"):
        return user["username"]
    return str(user.get("id"))


def ensure_group(db: dict, chat_id: int, title: str = ""):
    groups = db.setdefault("groups", {})
    key = str(chat_id)
    if key not in groups:
        groups[key] = {
            "title": title or "",
            "subscribers": {}
        }
    else:
        if title:
            groups[key]["title"] = title
    return groups[key]


def add_subscriber(db: dict, chat_id: int, title: str, user: dict):
    group = ensure_group(db, chat_id, title)
    subs = group.setdefault("subscribers", {})
    uid = str(user["id"])
    already_exists = uid in subs

    subs[uid] = {
        "name": get_user_display_name(user),
        "username": user.get("username", "")
    }
    return already_exists


def remove_subscriber(db: dict, chat_id: int, title: str, user_id: int):
    group = ensure_group(db, chat_id, title)
    subs = group.setdefault("subscribers", {})
    existed = str(user_id) in subs
    subs.pop(str(user_id), None)
    return existed


def list_subscribers(db: dict, chat_id: int):
    group = db.get("groups", {}).get(str(chat_id), {})
    return group.get("subscribers", {})


def build_html_mentions(subscribers: dict) -> str:
    parts = []
    for uid, info in subscribers.items():
        raw_name = info.get("name") or uid
        safe_name = html.escape(raw_name, quote=True)
        parts.append(f'<a href="tg://user?id={uid}">{safe_name}</a>')
    return "、".join(parts)


def is_admin(chat_id: int, user_id: int) -> bool:
    result = get_chat_member(chat_id, user_id)
    if not result.get("ok"):
        return False

    status = result.get("result", {}).get("status")
    return status in ("administrator", "creator")


def message_mentions_bot(message: dict) -> bool:
    text = message.get("text", "") or ""
    if not text:
        return False

    if f"@{BOT_USERNAME.lower()}" in text.lower():
        return True

    entities = message.get("entities", []) or []
    for ent in entities:
        if ent.get("type") == "mention":
            offset = ent.get("offset", 0)
            length = ent.get("length", 0)
            chunk = text[offset: offset + length]
            if chunk.lower() == f"@{BOT_USERNAME.lower()}":
                return True

    return False


def only_group_service_text(chat_id: int, msg_id: int):
    return send_message(chat_id, "此服務僅在群組中使用", reply_to_message_id=msg_id)


# -----------------------------
# 指令處理
# -----------------------------
def handle_command(message: dict):
    chat = message["chat"]
    chat_id = chat["id"]
    msg_id = message["message_id"]
    text = (message.get("text") or "").strip()
    user = message.get("from", {})

    cmd = text.split()[0].split("@")[0].lower()

    if is_private_chat(message):
        return only_group_service_text(chat_id, msg_id)

    if not is_group_chat(message):
        return jsonify({"ok": True})

    title = chat.get("title", "")

    if cmd == "/start":
        return send_message(chat_id, "此服務僅在群組中使用", reply_to_message_id=msg_id)

    if cmd == "/add":
        db = load_db()
        already_exists = add_subscriber(db, chat_id, title, user)
        save_db(db)
        delete_message(chat_id, msg_id)

        if already_exists:
            return send_message(chat_id, "你已經在推播通知名單中", parse_mode=None)
        return send_message(chat_id, "你已加入推播通知", parse_mode=None)

    if cmd == "/remove":
        db = load_db()
        remove_subscriber(db, chat_id, title, user["id"])
        save_db(db)
        delete_message(chat_id, msg_id)
        return jsonify({"ok": True})

    if cmd == "/list":
        db = load_db()
        subs = list_subscribers(db, chat_id)
        if not subs:
            return send_message(chat_id, "目前沒有任何人加入推播通知名單", reply_to_message_id=msg_id)

        names = []
        for _, info in subs.items():
            n = info.get("name") or "未知使用者"
            if info.get("username"):
                n += f" (@{info['username']})"
            names.append(f"• {n}")

        text_out = "目前推播通知名單：\n" + "\n".join(names)
        return send_message(chat_id, text_out, reply_to_message_id=msg_id)

    return jsonify({"ok": True})


# -----------------------------
# 非指令訊息：管理員標註 bot 觸發通知
# -----------------------------
def handle_regular_message(message: dict):
    if not is_group_chat(message):
        return jsonify({"ok": True})

    text = message.get("text", "") or ""
    if not text:
        return jsonify({"ok": True})

    if not message_mentions_bot(message):
        return jsonify({"ok": True})

    chat = message["chat"]
    chat_id = chat["id"]
    msg_id = message["message_id"]
    sender = message.get("from", {})
    sender_id = sender.get("id")

    # 非管理員：直接忽略，不回任何訊息
    if not sender_id or not is_admin(chat_id, sender_id):
        return jsonify({"ok": True})

    db = load_db()
    subs = list_subscribers(db, chat_id)

    if not subs:
        return send_message(chat_id, "目前沒有任何人加入推播通知名單", reply_to_message_id=msg_id)

    mentions = build_html_mentions(subs)
    notify_text = f"呼叫🚨\n{mentions}"

    return send_message(
        chat_id,
        notify_text,
        reply_to_message_id=msg_id,
        parse_mode="HTML"
    )


# -----------------------------
# 路由
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "service": "tg mention notify bot",
        "env_ok": env_ok()
    })


@app.route("/api/webhook/<secret>", methods=["POST"])
def webhook(secret):
    if not env_ok():
        return jsonify({"ok": False, "error": "Missing environment variables"}), 500

    if secret != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    update = request.get_json(silent=True) or {}

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    text = (message.get("text") or "").strip()

    if text.startswith("/"):
        handle_command(message)
    else:
        handle_regular_message(message)

    return jsonify({"ok": True})