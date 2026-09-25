from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from email.parser import BytesParser
from email.policy import default
from datetime import datetime, timedelta, timezone
import json, os, uuid, mimetypes, threading
import database
import migrate_to_postgres

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data.json")
UPLOADS = os.path.join(BASE, "uploads")
PORT = int(os.environ.get("PORT", "5050"))
ALLOWED = {".gif", ".jpg", ".jpeg", ".png", ".webp"}
MAX_BYTES = 12 * 1024 * 1024
LOCK = threading.Lock()
COUPON_EVENT_TYPES = {"view", "copy", "redeem"}

os.makedirs(UPLOADS, exist_ok=True)

def load_state():
    if database.database_enabled():
        return database.load_state()
    with open(DATA, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("coupons", [])
    data.setdefault("coupon_events", [])
    return data

def save_state(data, preserve_latest_events=True):
    if database.database_enabled():
        return database.save_state(data)
    # The admin page submits a cached snapshot; retain newer events recorded
    # after that snapshot was loaded.
    if preserve_latest_events:
        try:
            with open(DATA, "r", encoding="utf-8") as f:
                latest = json.load(f)
                data["events"] = latest.get("events", [])
                data["coupon_events"] = latest.get("coupon_events", [])
                data.setdefault("coupons", latest.get("coupons", []))
        except (OSError, json.JSONDecodeError):
            data.setdefault("events", [])
            data.setdefault("coupon_events", [])
    data.setdefault("coupons", [])
    data.setdefault("coupon_events", [])
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA)

def now_jst():
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=9)).isoformat(timespec="seconds")

def record_event(event_type, store, ad_id="main"):
    if database.database_enabled():
        return database.record_event(event_type, store, ad_id)
    with LOCK:
        data = load_state()
        # Resolve the store from the current ad configuration when the page
        # could not provide one (for example, while an old config is loading).
        if not store or store == "未設定":
            ad = data.get("ad", {})
            if str(ad.get("id") or "main") == str(ad_id):
                store = ad.get("store")
        events = data.setdefault("events", [])
        events.append({"type": event_type, "store": store or "未設定", "adId": ad_id, "at": now_jst()})
        # Keep prototype data manageable while retaining recent history.
        data["events"] = events[-10000:]
        save_state(data, preserve_latest_events=False)

def list_available_coupons(data=None):
    data = data or load_state()
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
    return [coupon for coupon in data.get("coupons", [])
            if coupon.get("status") == "active"
            and (not coupon.get("start") or coupon["start"] <= today)
            and (not coupon.get("end") or coupon["end"] >= today)]

def record_coupon_event(payload):
    if payload.get("type") not in COUPON_EVENT_TYPES:
        raise ValueError("invalid coupon event type")
    data = load_state()
    coupon_id = str(payload.get("couponId") or "")
    coupon = next((item for item in data.get("coupons", []) if str(item.get("id")) == coupon_id), None)
    if not coupon:
        raise LookupError("coupon not found")
    event = {
        "couponId": coupon_id,
        "couponCode": coupon.get("code", ""),
        "type": payload["type"],
        "store": coupon.get("store") or next(
            (store.get("name", "未設定") for store in data.get("stores", [])
             if str(store.get("id")) == str(coupon.get("storeId"))), "未設定"),
        "adId": coupon.get("adId") or data.get("ad", {}).get("id", "main"),
        "at": now_jst(),
    }
    if database.database_enabled():
        database.record_coupon_event(event)
        return
    with LOCK:
        data = load_state()
        if not any(str(item.get("id")) == coupon_id for item in data.get("coupons", [])):
            raise LookupError("coupon not found")
        events = data.setdefault("coupon_events", [])
        events.append(event)
        data["coupon_events"] = events[-10000:]
        save_state(data, preserve_latest_events=False)

def build_coupon_analytics(events):
    grouped = {}
    for event in events:
        key = (event.get("couponId"), event.get("couponCode", ""), event.get("type", "unknown"))
        grouped[key] = grouped.get(key, 0) + 1
    rows = [{"coupon_id": coupon_id, "coupon_code": coupon_code,
             "event_type": event_type, "total": total}
            for (coupon_id, coupon_code, event_type), total in grouped.items()]
    return database.build_coupon_analytics(rows)

class Handler(BaseHTTPRequestHandler):
    server_version = "WiFiMedia/2.0"
    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args))
    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)
    def send_json(self, obj, status=200):
        return self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status, "application/json; charset=utf-8")
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/": return self.serve_file("web.html")
        if path == "/admin": return self.serve_file("admin.html")
        if path == "/api/coupons":
            try: return self.send_json({"coupons": list_available_coupons()})
            except Exception as e: return self.send_json({"error": str(e)}, 500)
        if path == "/api/coupon_analytics":
            try:
                if database.database_enabled():
                    return self.send_json(database.coupon_analytics())
                return self.send_json(build_coupon_analytics(load_state().get("coupon_events", [])))
            except Exception as e: return self.send_json({"error": str(e)}, 500)
        if path == "/api/state":
            try: return self.send_json(load_state())
            except Exception as e: return self.send_json({"error": str(e)}, 500)
        if path == "/api/analytics":
            try:
                data = load_state(); events = data.get("events", [])
                return self.send_json(build_analytics(events))
            except Exception as e: return self.send_json({"error": str(e)}, 500)
        if path == "/health": return self.send_bytes(b"ok", 200, "text/plain; charset=utf-8")
        if path.startswith("/uploads/"):
            return self.serve_upload(os.path.basename(path))
        return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")
    def serve_file(self, name):
        path = os.path.join(BASE, name)
        if not os.path.isfile(path): return self.send_bytes(b"file not found", 404, "text/plain")
        with open(path, "rb") as f: body = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/"): ctype += "; charset=utf-8"
        return self.send_bytes(body, 200, ctype)
    def serve_upload(self, name):
        path = os.path.join(UPLOADS, name)
        if not os.path.isfile(path): return self.send_bytes(b"not found", 404, "text/plain")
        with open(path, "rb") as f: body = f.read()
        return self.send_bytes(body, 200, mimetypes.guess_type(path)[0] or "application/octet-stream")
    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BYTES: raise ValueError("file too large")
        return self.rfile.read(length)
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/coupon_event":
            try:
                payload = json.loads(self.read_body().decode("utf-8"))
                record_coupon_event(payload)
                return self.send_json({"ok": True})
            except LookupError as e: return self.send_json({"error": str(e)}, 404)
            except Exception as e: return self.send_json({"error": str(e)}, 400)
        if path == "/api/event":
            try:
                body = self.read_body(); payload = json.loads(body.decode("utf-8"))
                event_type = payload.get("type")
                if event_type not in {"impression", "click"}: return self.send_json({"error":"invalid event"}, 400)
                record_event(event_type, payload.get("store", ""), payload.get("adId", "main"))
                return self.send_json({"ok": True})
            except Exception as e: return self.send_json({"error": str(e)}, 400)
        if path == "/api/state":
            try:
                body = self.read_body(); data = json.loads(body.decode("utf-8")); save_state(data)
                return self.send_json({"ok": True})
            except Exception as e: return self.send_json({"error": str(e)}, 400)
        if path == "/api/upload":
            try:
                body = self.read_body(); ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype: return self.send_json({"error":"multipart/form-data required"}, 400)
                header_blob = ("Content-Type: " + ctype + "\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8")
                msg = BytesParser(policy=default).parsebytes(header_blob + body)
                part = next((p for p in msg.iter_parts() if p.get_param("name", header="Content-Disposition") == "file"), None)
                if part is None: return self.send_json({"error":"file not found"}, 400)
                ext = os.path.splitext(part.get_filename() or "")[1].lower()
                if ext not in ALLOWED: return self.send_json({"error":"gif/jpg/png/webp only"}, 400)
                content = part.get_payload(decode=True) or b""
                if len(content) > MAX_BYTES: return self.send_json({"error":"12MB以下のファイルにしてください"}, 400)
                name = uuid.uuid4().hex + ext
                with open(os.path.join(UPLOADS, name), "wb") as f: f.write(content)
                return self.send_json({"url":"/uploads/" + name})
            except Exception as e: return self.send_json({"error": str(e)}, 400)
        return self.send_json({"error":"not found"}, 404)

def build_analytics(events):
    impressions = [e for e in events if e.get("type") == "impression"]
    clicks = [e for e in events if e.get("type") == "click"]
    def by_store(items):
        out = {}
        for e in items: out[e.get("store", "未設定")] = out.get(e.get("store", "未設定"), 0) + 1
        return out
    days = {}
    for e in events:
        day = e.get("at", "")[:10]
        if not day: continue
        days.setdefault(day, {"impressions":0,"clicks":0})
        days[day]["impressions" if e.get("type")=="impression" else "clicks"] += 1
    return {
        "impressions": len(impressions), "clicks": len(clicks),
        "ctr": round((len(clicks)/len(impressions)*100), 2) if impressions else 0,
        "byStore": {k:{"impressions":by_store(impressions).get(k,0),"clicks":by_store(clicks).get(k,0)} for k in sorted(set(by_store(impressions)) | set(by_store(clicks)))},
        "daily": [{"date":k, **days[k], "ctr":round(days[k]["clicks"]/days[k]["impressions"]*100,2) if days[k]["impressions"] else 0} for k in sorted(days)]
    }

if __name__ == "__main__":
    if database.database_enabled():
        # The existing migration is guarded in database.import_legacy_state,
        # so it only imports data.json while PostgreSQL is still empty.
        migrate_to_postgres.main()
    print("Wi-Fi MEDIA サーバーを起動しています")
    print("管理画面: http://127.0.0.1:%d/admin" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
