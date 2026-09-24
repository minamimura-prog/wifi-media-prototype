from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from email.parser import BytesParser
from email.policy import default
import json, os, uuid, mimetypes

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data.json")
UPLOADS = os.path.join(BASE, "uploads")
PORT = int(os.environ.get("PORT", "5050"))
os.makedirs(UPLOADS, exist_ok=True)

ALLOWED = {".gif", ".jpg", ".jpeg", ".png", ".webp"}
MAX_BYTES = 12 * 1024 * 1024

def load_state():
    with open(DATA, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(data):
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA)

class Handler(BaseHTTPRequestHandler):
    server_version = "WiFiMedia/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args))

    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_bytes(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            status,
            "application/json; charset=utf-8",
        )

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            return self.serve_file("web.html")
        if path == "/admin":
            return self.serve_file("admin.html")
        if path == "/api/state":
            try:
                return self.send_json(load_state())
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
        if path == "/health":
            return self.send_bytes(b"ok", 200, "text/plain; charset=utf-8")
        if path.startswith("/uploads/"):
            name = os.path.basename(path)
            if name != path.split("/")[-1]:
                return self.send_bytes(b"not found", 404, "text/plain")
            return self.serve_upload(name)

        return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")

    def serve_file(self, name):
        path = os.path.join(BASE, name)
        if not os.path.isfile(path):
            return self.send_bytes(b"file not found", 404, "text/plain")
        with open(path, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        return self.send_bytes(body, 200, ctype)

    def serve_upload(self, name):
        path = os.path.join(UPLOADS, name)
        if not os.path.isfile(path):
            return self.send_bytes(b"not found", 404, "text/plain")
        with open(path, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return self.send_bytes(body, 200, ctype)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BYTES:
            raise ValueError("file too large")
        return self.rfile.read(length)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/state":
            try:
                body = self.read_body()
                data = json.loads(body.decode("utf-8"))
                save_state(data)
                return self.send_json({"ok": True})
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)

        if path == "/api/upload":
            try:
                body = self.read_body()
                ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype:
                    return self.send_json({"error": "multipart/form-data required"}, 400)

                header_blob = (
                    "Content-Type: " + ctype + "\r\n"
                    "MIME-Version: 1.0\r\n\r\n"
                ).encode("utf-8")
                msg = BytesParser(policy=default).parsebytes(header_blob + body)
                part = None
                for p in msg.iter_parts():
                    if p.get_param("name", header="Content-Disposition") == "file":
                        part = p
                        break
                if part is None:
                    return self.send_json({"error": "file not found"}, 400)

                original = part.get_filename() or ""
                ext = os.path.splitext(original)[1].lower()
                if ext not in ALLOWED:
                    return self.send_json({"error": "gif/jpg/png/webp only"}, 400)

                content = part.get_payload(decode=True) or b""
                if len(content) > MAX_BYTES:
                    return self.send_json({"error": "12MB以下のファイルにしてください"}, 400)

                name = uuid.uuid4().hex + ext
                with open(os.path.join(UPLOADS, name), "wb") as f:
                    f.write(content)

                return self.send_json({"url": "/uploads/" + name})
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)

        return self.send_json({"error": "not found"}, 404)

if __name__ == "__main__":
    print("")
    print("Wi-Fi MEDIA サーバーを起動しています")
    print("管理画面: http://127.0.0.1:%d/admin" % PORT)
    print("公開ページ: http://127.0.0.1:%d/" % PORT)
    print("終了するにはこの画面で Ctrl+C")
    print("")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
