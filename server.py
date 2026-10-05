from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote
from http.cookies import SimpleCookie
from email.parser import BytesParser
from email.policy import default
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import ipaddress
import hmac, json, os, secrets, uuid, mimetypes, threading
import re
import logging
import stat
import database
import migrate_to_postgres

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data.json")
UPLOADS = os.path.join(BASE, "uploads")
PORT = int(os.environ.get("PORT", "5050"))
ALLOWED = {".gif", ".jpg", ".jpeg", ".png", ".webp"}
MAX_BYTES = 12 * 1024 * 1024
ADMIN_CAMPAIGN_MAX_BYTES = 64 * 1024
ADMIN_CAMPAIGN_STATUSES = {"draft", "active"}
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~:-]{16,255}$")
LOCK = threading.Lock()
COUPON_EVENT_TYPES = {"view", "copy", "redeem"}
ADMIN_SESSION_COOKIE = "wifi_media_admin_session"
ADMIN_SESSION_MAX_AGE = 24 * 60 * 60
PUBLIC_CODE_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# The database CHECK constrains the format but not length. Keep a practical
# HTTP input bound while accepting manually assigned valid public codes.
PUBLIC_CODE_MAX_LENGTH = 128
COMPANY_CODE_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ADMIN_COMPANY_MAX_BYTES = 16 * 1024

def _safe_admin_route_id(raw_value):
    value = unquote(raw_value)
    if (not value or len(value) > 256 or "/" in value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    return value


def _admin_store_company_route(path):
    parts = path.split("/")
    if (len(parts) != 6 or parts[:4] != ["", "api", "admin", "stores"]
            or parts[5] != "company"):
        return None
    return _safe_admin_route_id(parts[4])


def _validate_admin_company(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    allowed = {"name", "code", "businessType", "status"}
    if not {"name", "code"}.issubset(payload) or not set(payload).issubset(allowed):
        raise ValueError("invalid fields")
    name = payload["name"]
    code = payload["code"]
    business_type = payload.get("businessType", "")
    status = payload.get("status", "active")
    if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 200
            or not isinstance(code, str) or not COMPANY_CODE_PATTERN.fullmatch(code)
            or len(code) > 100):
        raise ValueError("invalid name or code")
    if not isinstance(business_type, str) or len(business_type) > 100:
        raise ValueError("invalid businessType")
    if not isinstance(status, str) or status not in {"active", "inactive"}:
        raise ValueError("invalid status")
    return {
        "name": name.strip(), "code": code,
        "business_type": business_type.strip(), "status": status,
    }


def _admin_store_campaign_route(path, include_campaign=False):
    parts = path.split("/")
    expected_length = 7 if include_campaign else 6
    if (len(parts) != expected_length or parts[:4] != ["", "api", "admin", "stores"]
            or parts[5] != "campaigns"):
        return None
    store_id = _safe_admin_route_id(parts[4])
    campaign_id = _safe_admin_route_id(parts[6]) if include_campaign else None
    if store_id is None or (include_campaign and campaign_id is None):
        return None
    return store_id, campaign_id


def _parse_admin_campaign_date(value, field_name):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"invalid {field_name}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}") from exc


def _validate_admin_campaign(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    allowed = {
        "name", "status", "startsOn", "endsOn", "title", "body",
        "landingUrl", "mediaUrl", "mediaUrlMobile", "published",
    }
    required = {"name", "status", "title", "body", "landingUrl", "mediaUrl", "published"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise ValueError("invalid fields")

    text_limits = {
        "name": 200, "title": 500, "body": 24000,
        "landingUrl": 4096, "mediaUrl": 4096,
    }
    for key, limit in text_limits.items():
        value = payload[key]
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"invalid {key}")
    if "mediaUrlMobile" in payload:
        value = payload["mediaUrlMobile"]
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError("invalid mediaUrlMobile")
    if not payload["name"].strip() or not payload["title"].strip():
        raise ValueError("name and title are required")
    if (not isinstance(payload["status"], str)
            or payload["status"] not in ADMIN_CAMPAIGN_STATUSES):
        raise ValueError("invalid status")
    if type(payload["published"]) is not bool:
        raise ValueError("invalid published")

    starts_on = _parse_admin_campaign_date(payload.get("startsOn"), "startsOn")
    ends_on = _parse_admin_campaign_date(payload.get("endsOn"), "endsOn")
    if starts_on is not None and ends_on is not None and starts_on > ends_on:
        raise ValueError("invalid date range")
    campaign = {
        "name": payload["name"],
        "status": payload["status"],
        "starts_on": starts_on,
        "ends_on": ends_on,
        "title": payload["title"],
        "body": payload["body"],
        "landing_url": payload["landingUrl"],
        "media_url": payload["mediaUrl"],
        "published": payload["published"],
    }
    if "mediaUrlMobile" in payload:
        campaign["media_url_mobile"] = payload["mediaUrlMobile"]
    return campaign


def _read_idempotency_key(headers):
    """Return (key, valid); a missing header preserves legacy POST behavior."""
    values = headers.get_all("Idempotency-Key", [])
    if not values:
        return None, True
    if len(values) != 1:
        return None, False
    key = values[0]
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        return None, False
    return key, True


def _admin_campaign_json(row):
    def iso(value):
        return value.isoformat() if value is not None else None
    return {
        "campaignId": row["campaign_id"],
        "adId": row["ad_id"],
        "name": row["campaign_name"],
        "status": row["campaign_status"],
        "startsOn": iso(row.get("starts_on")),
        "endsOn": iso(row.get("ends_on")),
        "title": row["title"],
        "body": row["body"],
        "landingUrl": row["landing_url"],
        "mediaUrl": row["media_url"],
        "mediaUrlMobile": row.get("media_url_mobile") or "",
        "published": row["published"],
        "createdAt": iso(row.get("campaign_created_at")),
        "updatedAt": iso(row.get("campaign_updated_at")),
    }


def admin_development_mode():
    """Allow HTTP cookies only when both development switches are explicit."""
    return (
        os.environ.get("APP_ENV", "").strip().lower() == "development"
        and os.environ.get("ADMIN_DEV_ALLOW_INSECURE_COOKIE", "").strip() == "1"
    )

def admin_session_cookie(token, max_age=ADMIN_SESSION_MAX_AGE):
    secure = "; Secure" if not admin_development_mode() else ""
    return (
        f"{ADMIN_SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; "
        f"Path=/; Max-Age={max_age}{secure}"
    )

def expired_admin_session_cookie():
    return admin_session_cookie("", max_age=0) + "; Expires=Thu, 01 Jan 1970 00:00:00 GMT"

os.makedirs(UPLOADS, exist_ok=True)

R2_CONFIG_ENV = (
    "R2_BUCKET_NAME",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_ACCOUNT_ID",
    "R2_PUBLIC_BASE_URL",
)
R2_CLIENT = None
R2_CLIENT_LOCK = threading.Lock()
R2_IMAGE_TYPES = {
    "jpeg": (".jpg", "image/jpeg"),
    "png": (".png", "image/png"),
    "gif": (".gif", "image/gif"),
    "webp": (".webp", "image/webp"),
}


class R2StorageConfigurationError(Exception):
    pass


def _detect_upload_image(content):
    """Identify supported raster image signatures without trusting filenames."""
    if not content:
        raise ValueError("empty image")
    if len(content) >= 24 and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return R2_IMAGE_TYPES["png"]
    if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
        return R2_IMAGE_TYPES["gif"]
    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return R2_IMAGE_TYPES["jpeg"]
    if (len(content) >= 12 and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"):
        return R2_IMAGE_TYPES["webp"]
    raise ValueError("unsupported image")


def _r2_settings():
    settings = {key: os.environ.get(key, "").strip() for key in R2_CONFIG_ENV}
    if any(not value for value in settings.values()):
        raise R2StorageConfigurationError("R2 configuration unavailable")
    public_base = settings["R2_PUBLIC_BASE_URL"].rstrip("/")
    parsed_base = urlparse(public_base)
    if (parsed_base.scheme != "https" or not parsed_base.netloc
            or parsed_base.username or parsed_base.password
            or parsed_base.query or parsed_base.fragment):
        raise R2StorageConfigurationError("R2 public URL configuration invalid")
    settings["R2_PUBLIC_BASE_URL"] = public_base
    return settings


def _r2_client(settings):
    global R2_CLIENT
    if R2_CLIENT is None:
        with R2_CLIENT_LOCK:
            if R2_CLIENT is None:
                import boto3
                R2_CLIENT = boto3.client(
                    "s3",
                    endpoint_url=(
                        f"https://{settings['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
                    ),
                    aws_access_key_id=settings["R2_ACCESS_KEY_ID"],
                    aws_secret_access_key=settings["R2_SECRET_ACCESS_KEY"],
                    region_name="auto",
                )
    return R2_CLIENT


def _store_uploaded_image(content, extension, content_type):
    name = uuid.uuid4().hex + extension
    is_render_service = os.environ.get("RENDER", "").strip().lower() == "true"
    if admin_development_mode() and not is_render_service:
        with open(os.path.join(UPLOADS, name), "wb") as image_file:
            image_file.write(content)
        return "/uploads/" + name

    settings = _r2_settings()
    object_key = "uploads/" + name
    _r2_client(settings).put_object(
        Bucket=settings["R2_BUCKET_NAME"],
        Key=object_key,
        Body=content,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )
    return settings["R2_PUBLIC_BASE_URL"] + "/" + object_key

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
    requested_store = (data.get("ad") or {}).get("store")
    requested_store_record = next(
        (store for store in data.get("stores", [])
         if store.get("name") == requested_store),
        None,
    )
    try:
        with open(DATA, "r", encoding="utf-8") as f:
            current_state = json.load(f)
    except (OSError, json.JSONDecodeError):
        current_state = {}
    current_store = (current_state.get("ad") or {}).get("store")
    current_store_record = next(
        (store for store in current_state.get("stores", [])
         if store.get("name") == requested_store),
        None,
    )
    target_is_not_active = bool(
        requested_store_record and requested_store_record.get("status") != "稼働中"
    ) or bool(
        current_store_record and current_store_record.get("status") != "稼働中"
    )
    if target_is_not_active and current_store != requested_store:
        raise database.StoreInactiveError("store_inactive")
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
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")

def _store_is_unavailable(store):
    if not store:
        return False
    archived_at = store.get("archivedAt", store.get("archived_at"))
    return store.get("status") == "停止中" or archived_at is not None

def _json_default_ad_target_is_available(data, ad):
    stores = data.get("stores", [])
    target_store_id = ad.get("storeId")
    if target_store_id is None:
        target_store_id = ad.get("store_id")
    if target_store_id is not None:
        if not str(target_store_id).strip():
            return False
        matches = [store for store in stores
                   if str(store.get("id") or "") == str(target_store_id)]
        return len(matches) == 1 and not _store_is_unavailable(matches[0])

    target_name = ad.get("store")
    # "未設定" is the existing JSON/API representation of an unassigned,
    # shared default ad. A missing or unresolvable store name is ambiguous.
    if target_name == "未設定":
        matches = [store for store in stores if store.get("name") == target_name]
        if not matches:
            return True
        return len(matches) == 1 and not _store_is_unavailable(matches[0])
    if not isinstance(target_name, str) or not target_name:
        return False
    matches = [store for store in stores if store.get("name") == target_name]
    return len(matches) == 1 and not _store_is_unavailable(matches[0])

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
        resolved_store = next(
            (item for item in data.get("stores", []) if item.get("name") == (store or "未設定")),
            None,
        )
        if _store_is_unavailable(resolved_store):
            raise database.StoreInactiveError("store_inactive")
        events = data.setdefault("events", [])
        events.append({
            "type": event_type,
            "store": (resolved_store.get("name") if resolved_store else store) or "未設定",
            "storeId": str(resolved_store.get("id")) if resolved_store else None,
            "companyId": resolved_store.get("companyId") if resolved_store else None,
            "companyAttributionStatus": "captured" if resolved_store else "store_unresolved",
            "adId": ad_id,
            "at": now_jst(),
        })
        # Keep prototype data manageable while retaining recent history.
        data["events"] = events[-10000:]
        save_state(data, preserve_latest_events=False)

def list_available_coupons(data=None, *, global_only=False):
    data = data or load_state()
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
    return [coupon for coupon in data.get("coupons", [])
            if coupon.get("status") == "active"
            and (not coupon.get("start") or coupon["start"] <= today)
            and (not coupon.get("end") or coupon["end"] >= today)
            and (not global_only or not (coupon.get("storeId") or coupon.get("store_id")))]

def public_config():
    data = load_state()
    design = data.get("design") or {}
    ad = data.get("ad") or {}
    if database.database_enabled():
        target_store = database.get_default_ad_target_store()
        target_is_available = not _store_is_unavailable(target_store)
    else:
        target_is_available = _json_default_ad_target_is_available(data, ad)
    if not target_is_available:
        ad = {}
    return {
        "design": {key: design.get(key) for key in (
            "background", "textColor", "radius", "heroHeight", "pageTitle",
            "brand", "buttonText", "buttonColor", "buttonTextColor",
            "showBrand", "showBody", "showButton",
        )},
        "ad": {key: (ad.get(key) or "") if key == "mediaMobile" else ad.get(key)
               for key in (
                   "title", "body", "link", "media", "store", "id", "mediaMobile",
               )},
    }

def _campaign_date(value):
    """Parse a PostgreSQL DATE or ISO date string without timezone conversion."""
    if value is None:
        return None, True
    if isinstance(value, datetime):
        return value.date(), True
    if isinstance(value, date):
        return value, True
    if isinstance(value, str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None, False
        try:
            return date.fromisoformat(value), True
        except ValueError:
            return None, False
    return None, False

def _campaign_is_deliverable(campaign, today):
    if campaign.get("campaign_status") != "active" or campaign.get("published") is not True:
        return False
    starts_on, valid_start = _campaign_date(campaign.get("starts_on"))
    ends_on, valid_end = _campaign_date(campaign.get("ends_on"))
    if not valid_start or not valid_end:
        return False
    return (starts_on is None or starts_on <= today) and (ends_on is None or today <= ends_on)

def public_config_for_store(public_code):
    """Build public config for one PostgreSQL-resolved store, without global ad fallback."""
    store = database.get_store_by_public_code(public_code)
    if not store:
        return None

    # Reuse the established global design settings; only replace its ad with a
    # campaign assigned through campaign_stores for this exact store.
    result = public_config()
    result["storeStatus"] = store.get("status")
    if _store_is_unavailable(store):
        result["ad"] = None
        return result

    # Campaign columns are DATE values; use the current service market's date.
    # This can later be replaced with a store-specific timezone.
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    campaigns = database.get_campaigns_for_store(store["id"])
    eligible = [item for item in campaigns if _campaign_is_deliverable(item, today)]
    if len(eligible) != 1:
        result["ad"] = None
        return result

    campaign = eligible[0]
    result["ad"] = {
        "title": campaign.get("title"),
        "body": campaign.get("body"),
        "link": campaign.get("landing_url"),
        "media": campaign.get("media_url"),
        "mediaMobile": campaign.get("media_url_mobile") or "",
        "store": store.get("name") or "未設定",
        "id": campaign.get("ad_id"),
        # Public opaque identifier used only to validate event attribution.
        "campaignId": campaign.get("campaign_id"),
    }
    return result

def public_coupons_for_store(public_code):
    """Return active coupons available to one active public store."""
    state = None
    if database.database_enabled():
        store = database.get_store_by_public_code(public_code)
    else:
        state = load_state()
        store = next(
            (item for item in state.get("stores", [])
             if item.get("publicCode") == public_code),
            None,
        )
    if not store:
        return None
    if _store_is_unavailable(store):
        return []

    store_id = store.get("id")
    if store_id is None:
        return None
    available = list_available_coupons(state)
    return [
        coupon for coupon in available
        if not coupon.get("storeId")
        or str(coupon.get("storeId")) == str(store_id)
    ]

def delete_draft_coupon(coupon_id):
    """Delete only a draft coupon; coupon_events remain available as history."""
    coupon_id = str(coupon_id)
    if database.database_enabled():
        return database.delete_draft_coupon(coupon_id)
    with LOCK:
        data = load_state()
        coupons = data.get("coupons", [])
        target = next((item for item in coupons if str(item.get("id")) == coupon_id), None)
        if not target or target.get("status") != "draft":
            return False
        data["coupons"] = [item for item in coupons if str(item.get("id")) != coupon_id]
        save_state(data)
        return True

def record_coupon_event(payload):
    if payload.get("type") not in COUPON_EVENT_TYPES:
        raise ValueError("invalid coupon event type")
    store_code = payload.get("storeCode")
    if store_code is not None and (
        not isinstance(store_code, str)
        or len(store_code) > PUBLIC_CODE_MAX_LENGTH
        or not PUBLIC_CODE_PATTERN.fullmatch(store_code)
    ):
        raise ValueError("invalid store code")
    data = load_state()
    coupon_id = str(payload.get("couponId") or "")
    coupon = next((item for item in data.get("coupons", []) if str(item.get("id")) == coupon_id), None)
    if not coupon:
        raise LookupError("coupon not found")
    event_store = next(
        (item for item in data.get("stores", [])
         if (coupon.get("storeId") and str(item.get("id")) == str(coupon.get("storeId")))
         or (not coupon.get("storeId") and item.get("name") == (coupon.get("store") or "未設定"))),
        None,
    )
    postgres_enabled = database.database_enabled()
    page_store = None
    if store_code and not postgres_enabled:
        page_store = next(
            (item for item in data.get("stores", []) if item.get("publicCode") == store_code),
            None,
        )
        if not page_store:
            raise LookupError("store not found")
        if _store_is_unavailable(page_store):
            raise database.StoreInactiveError("store_inactive")
    if _store_is_unavailable(event_store):
        raise database.StoreInactiveError("store_inactive")
    if (not postgres_enabled and coupon.get("storeId") and page_store
            and str(coupon["storeId"]) != str(page_store.get("id"))):
        raise database.StoreMismatchError("store_mismatch")
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
    if store_code:
        event["storeCode"] = store_code
    if postgres_enabled:
        database.record_coupon_event(event)
        return
    with LOCK:
        data = load_state()
        current_coupon = next(
            (item for item in data.get("coupons", []) if str(item.get("id")) == coupon_id),
            None,
        )
        if not current_coupon:
            raise LookupError("coupon not found")
        current_store = next(
            (item for item in data.get("stores", [])
             if (current_coupon.get("storeId")
                 and str(item.get("id")) == str(current_coupon.get("storeId")))
             or (not current_coupon.get("storeId")
                 and item.get("name") == (current_coupon.get("store") or "未設定"))),
            None,
        )
        if _store_is_unavailable(current_store):
            raise database.StoreInactiveError("store_inactive")
        current_page_store = None
        if store_code:
            current_page_store = next(
                (item for item in data.get("stores", []) if item.get("publicCode") == store_code),
                None,
            )
            if not current_page_store:
                raise LookupError("store not found")
            if _store_is_unavailable(current_page_store):
                raise database.StoreInactiveError("store_inactive")
        if (current_coupon.get("storeId") and current_page_store
                and str(current_coupon["storeId"]) != str(current_page_store.get("id"))):
            raise database.StoreMismatchError("store_mismatch")
        event_store = None
        if current_coupon.get("storeId"):
            event_store = next(
                (item for item in data.get("stores", [])
                 if str(item.get("id")) == str(current_coupon.get("storeId"))),
                None,
            )
        elif current_page_store:
            # A global coupon viewed on a store page belongs to that access store.
            event_store = current_page_store
        attribution_store = event_store if event_store else None
        event = {
            "couponId": coupon_id,
            "couponCode": current_coupon.get("code", ""),
            "type": payload["type"],
            "store": (attribution_store.get("name") if attribution_store else "未設定"),
            "storeId": str(attribution_store.get("id")) if attribution_store else None,
            "companyId": attribution_store.get("companyId") if attribution_store else None,
            "companyAttributionStatus": "captured" if attribution_store else "store_unresolved",
            "adId": current_coupon.get("adId") or data.get("ad", {}).get("id", "main"),
            "at": now_jst(),
        }
        if store_code:
            event["storeCode"] = store_code
        events = data.setdefault("coupon_events", [])
        events.append(event)
        data["coupon_events"] = events[-10000:]
        save_state(data, preserve_latest_events=False)

def _json_period_coupon_details(events, spec, coupon_names=None):
    jst = ZoneInfo("Asia/Tokyo")
    names = coupon_names if isinstance(coupon_names, dict) else {}
    grouped = {}
    for event in events:
        event_type = event.get("type", event.get("event_type"))
        if event_type not in {"view", "copy", "redeem"}:
            continue
        raw_coupon_id = event.get("couponId", event.get("coupon_id"))
        if raw_coupon_id is None:
            continue
        coupon_id = str(raw_coupon_id).strip()
        if not coupon_id:
            continue
        raw_at = event.get("at", event.get("occurred_at"))
        if not raw_at:
            continue
        try:
            event_at = raw_at if isinstance(raw_at, datetime) else datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=jst)
        event_at = event_at.astimezone(jst)
        if not (spec["start"] <= event_at < spec["end"]):
            continue
        raw_name = names.get(coupon_id)
        coupon_name = str(raw_name).strip() if raw_name is not None else ""
        item = grouped.setdefault(coupon_id, {
            "couponId": coupon_id,
            "couponName": coupon_name or coupon_id,
            "views": 0,
            "copies": 0,
            "redeems": 0,
        })
        item[{"view": "views", "copy": "copies", "redeem": "redeems"}[event_type]] += 1
    details = list(grouped.values())
    for item in details:
        views = item["views"]
        item["copyRate"] = round(item["copies"] / views * 100, 2) if views else 0
        item["redeemRate"] = round(item["redeems"] / views * 100, 2) if views else 0
    details.sort(key=lambda item: (
        -item["redeems"], -item["copies"], -item["views"],
        -item["redeemRate"], item["couponName"], item["couponId"],
    ))
    return details


def build_coupon_analytics(events, period=None, coupon_names=None):
    grouped = {}
    for event in events:
        key = (event.get("couponId"), event.get("couponCode", ""), event.get("type", "unknown"))
        grouped[key] = grouped.get(key, 0) + 1
    rows = [{"coupon_id": coupon_id, "coupon_code": coupon_code,
             "event_type": event_type, "total": total}
            for (coupon_id, coupon_code, event_type), total in grouped.items()]
    result = database.build_coupon_analytics(rows)
    if period is not None:
        spec = database.performance_period_spec(period)
        result["periodCouponDetails"] = _json_period_coupon_details(events, spec, coupon_names)
    return result


def _analytics_company_filter(query):
    """Validate the optional companyId query parameter's shape."""
    if set(query) - {"companyId"} or any(len(values) != 1 for values in query.values()):
        return None, ("invalid_scope", 400)
    if "companyId" not in query:
        return None, None
    company_id = query["companyId"][0]
    if (not company_id.strip() or len(company_id) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in company_id)):
        return None, ("invalid_company_id", 400)
    return company_id, None


def _company_exists_in_state(data, company_id):
    return any(
        isinstance(company, dict) and str(company.get("id", "")) == company_id
        for company in data.get("companies", [])
    )


def _events_for_company(events, company_id):
    """Filter by event-time attribution only; never infer from current stores."""
    if company_id is None:
        return events
    return [
        event for event in events
        if str(event.get("companyId")) == company_id
        and event.get("companyAttributionStatus") == "captured"
    ]

class Handler(BaseHTTPRequestHandler):
    server_version = "WiFiMedia/2.0"
    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args))
    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items(): self.send_header(name, value)
        self.end_headers(); self.wfile.write(body)
    def send_json(self, obj, status=200, extra_headers=None):
        return self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status, "application/json; charset=utf-8", extra_headers)
    def serve_qr_library(self):
        """Serve only the pinned QR library, without mapping URL paths to files."""
        static_dir = os.path.join(BASE, "static")
        vendor_dir = os.path.join(static_dir, "vendor")
        asset_path = os.path.join(vendor_dir, "qrcode-generator-2.0.4.js")
        try:
            if any(os.path.islink(path) for path in (static_dir, vendor_dir, asset_path)):
                return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")
            static_real = os.path.realpath(static_dir)
            vendor_real = os.path.realpath(vendor_dir)
            asset_real = os.path.realpath(asset_path)
            if (os.path.commonpath((BASE, static_real)) != BASE
                    or os.path.commonpath((static_real, vendor_real)) != static_real
                    or os.path.commonpath((vendor_real, asset_real)) != vendor_real):
                return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")
            descriptor = os.open(asset_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")
                with os.fdopen(descriptor, "rb") as asset_file:
                    descriptor = -1
                    body = asset_file.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except (OSError, ValueError):
            return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")
        return self.send_bytes(
            body, 200, "application/javascript; charset=utf-8",
            {"X-Content-Type-Options": "nosniff",
             "Cache-Control": "public, max-age=31536000, immutable"},
        )
    def admin_session_token(self):
        cookies = SimpleCookie()
        try: cookies.load(self.headers.get("Cookie", ""))
        except Exception: return None
        morsel = cookies.get(ADMIN_SESSION_COOKIE)
        return morsel.value if morsel else None
    def has_valid_admin_session(self):
        """Return whether the request carries a valid administrator session."""
        token = self.admin_session_token()
        if not token: return False
        try: return database.is_admin_session_valid(token)
        except Exception: return False
    def require_admin_session(self):
        if self.has_valid_admin_session(): return True
        self.send_json({"ok": False, "error": "unauthorized"}, 401)
        return False
    def request_origin_is_same(self):
        origins = self.headers.get_all("Origin", [])
        hosts = self.headers.get_all("Host", [])
        forwarded_protocols = self.headers.get_all("X-Forwarded-Proto", [])
        if len(origins) != 1 or len(hosts) != 1 or len(forwarded_protocols) > 1:
            return False
        origin = origins[0]
        host = hosts[0].strip()
        if not origin or not host or "," in host or any(char.isspace() for char in host):
            return False
        expected_scheme = "http" if admin_development_mode() else "https"
        if forwarded_protocols:
            forwarded_scheme = forwarded_protocols[0].strip().lower()
            if "," in forwarded_scheme or forwarded_scheme not in {"http", "https"}:
                return False
            if not admin_development_mode() and forwarded_scheme != "https":
                return False
            expected_scheme = forwarded_scheme
        try:
            parsed = urlparse(origin)
            host_parts = urlparse(f"{expected_scheme}://{host}")
            parsed.port
            host_parts.port
        except ValueError:
            return False
        return (
            parsed.scheme.lower() == expected_scheme
            and parsed.netloc.lower() == host.lower()
            and parsed.path in ("", "/")
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and host_parts.netloc.lower() == host.lower()
            and not host_parts.username and not host_parts.password
            and host_parts.path in ("", "/")
            and not host_parts.query and not host_parts.fragment
        )
    def require_same_origin(self):
        if self.request_origin_is_same(): return True
        self.send_json({"ok": False, "error": "forbidden"}, 403)
        return False
    def read_admin_campaign_payload(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None, 415
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return None, 400
        if length <= 0:
            return None, 400
        if length > ADMIN_CAMPAIGN_MAX_BYTES:
            return None, 413
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                return None, 400
            raw_payload = json.loads(body.decode("utf-8"))
            return _validate_admin_campaign(raw_payload), None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None, 400
    def read_admin_json_payload(self, max_bytes=ADMIN_COMPANY_MAX_BYTES):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None, 415
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return None, 400
        if length <= 0:
            return None, 400
        if length > max_bytes:
            return None, 413
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                return None, 400
            return json.loads(body.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, 400
    def admin_login_client_ip(self):
        if os.environ.get("RENDER", "").strip().lower() == "true":
            values = self.headers.get_all("CF-Connecting-IP", [])
            if len(values) != 1:
                return None
            raw_ip = values[0]
        elif os.environ.get("APP_ENV", "").strip().lower() == "development":
            address = getattr(self, "client_address", None)
            raw_ip = address[0] if isinstance(address, tuple) and address else None
        else:
            return None
        if not isinstance(raw_ip, str) or not raw_ip or raw_ip != raw_ip.strip() or "%" in raw_ip:
            return None
        try:
            return str(ipaddress.ip_address(raw_ip))
        except ValueError:
            return None
    def handle_admin_login(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            return self.send_json({"ok": False}, 415)
        try: content_length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError): return self.send_json({"ok": False}, 400)
        if content_length < 0: return self.send_json({"ok": False}, 400)
        if content_length > 4096: return self.send_json({"ok": False}, 413)
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.send_json({"ok": False}, 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("password"), str):
            return self.send_json({"ok": False}, 400)
        ip_address = self.admin_login_client_ip()
        if ip_address is None:
            return self.send_json({"ok": False}, 401)
        configured_password = os.environ.get("ADMIN_PASSWORD")
        if not configured_password:
            return self.send_json({"ok": False}, 401)
        try:
            if database.is_admin_login_ip_locked(ip_address):
                return self.send_json({"ok": False}, 401)
        except Exception:
            return self.send_json({"ok": False}, 503)
        if not hmac.compare_digest(payload["password"].encode("utf-8"), configured_password.encode("utf-8")):
            try:
                database.record_admin_login_failure(ip_address)
            except Exception:
                return self.send_json({"ok": False}, 503)
            return self.send_json({"ok": False}, 401)
        try:
            database.reset_admin_login_failures(ip_address)
        except Exception:
            return self.send_json({"ok": False}, 503)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ADMIN_SESSION_MAX_AGE)
        try:
            database.create_admin_session(token, expires_at)
        except Exception:
            return self.send_json({"ok": False}, 503)
        try:
            database.cleanup_admin_login_limits()
        except Exception:
            pass
        return self.send_json(
            {"ok": True},
            extra_headers={"Set-Cookie": admin_session_cookie(token)},
        )
    def handle_admin_logout(self):
        if not self.require_same_origin(): return
        token = self.admin_session_token()
        expired_cookie = {"Set-Cookie": expired_admin_session_cookie()}
        try:
            if token: database.revoke_admin_session(token)
        except Exception:
            return self.send_json({"ok": False}, 503, extra_headers=expired_cookie)
        return self.send_json({"ok": True}, extra_headers=expired_cookie)
    def do_GET(self):
        path = urlparse(self.path).path
        if self.path == "/static/qrcode-generator-2.0.4.js": return self.serve_qr_library()
        if path == "/": return self.serve_file("web.html")
        if path == "/admin/login":
            if self.has_valid_admin_session():
                return self.send_bytes(b"", 302, "text/plain; charset=utf-8", {"Location": "/admin"})
            return self.serve_file("login.html")
        if path == "/admin":
            if not self.has_valid_admin_session():
                return self.send_bytes(b"", 302, "text/plain; charset=utf-8", {"Location": "/admin/login"})
            return self.serve_file("admin.html")
        if path == "/api/admin/stores":
            if not self.require_admin_session(): return
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
            try:
                stores = database.list_admin_stores()
                return self.send_json({"stores": [
                    {"id": row["id"], "name": row["name"], "publicCode": row["public_code"],
                     "companyId": row.get("company_id"), "status": row.get("status"),
                     "archivedAt": row["archived_at"].isoformat() if row.get("archived_at") else None}
                    for row in stores
                ]})
            except Exception:
                return self.send_json({"ok": False}, 503)
        if path == "/api/admin/companies":
            if not self.require_admin_session(): return
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
            try:
                companies = database.list_companies()
                return self.send_json({"companies": [
                    {"id": row["id"], "name": row["name"], "code": row["code"],
                     "businessType": row["business_type"], "status": row["status"]}
                    for row in companies
                ]})
            except Exception:
                return self.send_json({"ok": False}, 503)
        store_route = _admin_store_campaign_route(path)
        if store_route:
            if not self.require_admin_session(): return
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
            store_id, _ = store_route
            try:
                store = database.get_store_by_id(store_id)
                if not store:
                    return self.send_json({"ok": False, "error": "not_found"}, 404)
                campaigns = database.get_admin_campaigns_for_store(store_id)
                return self.send_json({
                    "store": {"id": store["id"], "name": store["name"], "publicCode": store["public_code"],
                              "archivedAt": store["archived_at"].isoformat() if store.get("archived_at") else None},
                    "campaigns": [_admin_campaign_json(row) for row in campaigns],
                })
            except Exception:
                return self.send_json({"ok": False}, 503)
        if path == "/api/public-config":
            try:
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True, max_num_fields=100)
            except ValueError:
                return self.send_json({"ok": False, "error": "invalid_request"}, 400)
            if "store" not in query:
                try: return self.send_json(public_config())
                except Exception as e: return self.send_json({"error": str(e)}, 500)

            store_values = query["store"]
            if (len(store_values) != 1 or not store_values[0]
                    or len(store_values[0]) > PUBLIC_CODE_MAX_LENGTH
                    or not PUBLIC_CODE_PATTERN.fullmatch(store_values[0])):
                return self.send_json({"ok": False, "error": "invalid_store"}, 400)
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "store_delivery_unavailable"}, 503)
            try:
                config = public_config_for_store(store_values[0])
            except Exception:
                return self.send_json({"ok": False, "error": "store_delivery_unavailable"}, 503)
            if config is None:
                return self.send_json({"ok": False, "error": "store_not_found"}, 404)
            return self.send_json(config)
        if path == "/api/coupons":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if "store" in query:
                store_values = query["store"]
                if (len(store_values) != 1 or not store_values[0]
                        or len(store_values[0]) > PUBLIC_CODE_MAX_LENGTH
                        or not PUBLIC_CODE_PATTERN.fullmatch(store_values[0])):
                    return self.send_json({"ok": False, "error": "invalid_store"}, 400)
                try:
                    coupons = public_coupons_for_store(store_values[0])
                except Exception:
                    return self.send_json({"ok": False, "error": "store_delivery_unavailable"}, 503)
                if coupons is None:
                    return self.send_json({"ok": False, "error": "store_not_found"}, 404)
                return self.send_json({"coupons": coupons})
            try: return self.send_json({"coupons": list_available_coupons(global_only=True)})
            except Exception as e: return self.send_json({"error": str(e)}, 500)
        if path == "/api/coupon_analytics":
            if not self.require_admin_session(): return
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if set(query) - {"companyId", "period"} or any(len(values) != 1 for values in query.values()):
                return self.send_json({"ok": False, "error": "invalid_scope"}, 400)
            company_query = {"companyId": query["companyId"]} if "companyId" in query else {}
            company_id, error = _analytics_company_filter(company_query)
            if error:
                return self.send_json({"ok": False, "error": error[0]}, error[1])
            period = query["period"][0] if "period" in query else None
            if period is not None and period not in {"yesterday", "7d", "30d", "12m"}:
                return self.send_json({"ok": False, "error": "invalid_period"}, 400)
            try:
                if database.database_enabled():
                    if company_id is not None and not database.get_company(company_id):
                        return self.send_json({"ok": False, "error": "company_not_found"}, 404)
                    return self.send_json(database.coupon_analytics(company_id, period))
                data = load_state()
                if company_id is not None and not _company_exists_in_state(data, company_id):
                    return self.send_json({"ok": False, "error": "company_not_found"}, 404)
                events = _events_for_company(data.get("coupon_events", []), company_id)
                coupon_names = {
                    str(coupon.get("id")): coupon.get("title", "")
                    for coupon in data.get("coupons", [])
                    if isinstance(coupon, dict) and coupon.get("id") is not None
                }
                return self.send_json(build_coupon_analytics(events, period, coupon_names))
            except Exception: return self.send_json({"ok": False}, 500)
        if path == "/api/state":
            if not self.require_admin_session(): return
            try: return self.send_json(load_state())
            except Exception: return self.send_json({"ok": False}, 500)
        if path == "/api/admin/dashboard-analytics":
            if not self.require_admin_session(): return
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "dashboard_analytics_unavailable"}, 503)
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()) or set(query) - {"scope", "companyId"}:
                return self.send_json({"ok": False, "error": "invalid_scope"}, 400)
            if "scope" in query:
                scope = query["scope"][0]
                if scope not in {"all", "unassigned"} or "companyId" in query:
                    return self.send_json({"ok": False, "error": "invalid_scope"}, 400)
                company_id = None
            else:
                company_id = query.get("companyId", [""])[0]
                if (not company_id.strip() or len(company_id) > 256
                        or any(ord(char) < 32 or ord(char) == 127 for char in company_id)):
                    return self.send_json({"ok": False, "error": "invalid_company_id"}, 400)
                try:
                    company = database.get_company(company_id)
                except Exception:
                    return self.send_json({"ok": False}, 503)
                if not company:
                    return self.send_json({"ok": False, "error": "company_not_found"}, 404)
                scope = "company"
            try:
                return self.send_json(database.dashboard_analytics(scope, company_id))
            except ValueError:
                return self.send_json({"ok": False, "error": "invalid_scope"}, 400)
            except Exception:
                return self.send_json({"ok": False}, 503)
        if path == "/api/analytics":
            if not self.require_admin_session(): return
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if set(query) - {"companyId", "period"} or any(len(values) != 1 for values in query.values()):
                return self.send_json({"ok": False, "error": "invalid_scope"}, 400)
            company_query = {"companyId": query["companyId"]} if "companyId" in query else {}
            company_id, error = _analytics_company_filter(company_query)
            if error:
                return self.send_json({"ok": False, "error": error[0]}, error[1])
            period = query["period"][0] if "period" in query else None
            if period is not None and period not in {"yesterday", "7d", "30d", "12m"}:
                return self.send_json({"ok": False, "error": "invalid_period"}, 400)
            try:
                if database.database_enabled():
                    if company_id is not None and not database.get_company(company_id):
                        return self.send_json({"ok": False, "error": "company_not_found"}, 404)
                    return self.send_json(database.analytics(company_id, period))
                data = load_state()
                if company_id is not None and not _company_exists_in_state(data, company_id):
                    return self.send_json({"ok": False, "error": "company_not_found"}, 404)
                events = _events_for_company(data.get("events", []), company_id)
                ad_names = {}
                current_ad = data.get("ad") or {}
                if isinstance(current_ad, dict):
                    current_ad_id = str(current_ad.get("id") or "main").strip()
                    current_ad_name = current_ad.get("title")
                    if current_ad_id and isinstance(current_ad_name, str) and current_ad_name.strip():
                        ad_names[current_ad_id] = current_ad_name.strip()
                return self.send_json(build_analytics(events, period, ad_names))
            except Exception: return self.send_json({"ok": False}, 500)
        if path == "/health": return self.send_bytes(b"ok", 200, "text/plain; charset=utf-8")
        if path.startswith("/uploads/"):
            return self.serve_upload(os.path.basename(path))
        return self.send_bytes(b"not found", 404, "text/plain; charset=utf-8")
    def do_PUT(self):
        path = urlparse(self.path).path
        company_store_id = _admin_store_company_route(path)
        if company_store_id is not None:
            if not self.require_admin_session(): return
            if not self.require_same_origin(): return
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
            payload, error_status = self.read_admin_json_payload()
            if error_status:
                return self.send_json({"ok": False, "error": "invalid_request"}, error_status)
            if not isinstance(payload, dict) or set(payload) != {"companyId"}:
                return self.send_json({"ok": False, "error": "invalid_request"}, 400)
            requested_company_id = payload["companyId"]
            if requested_company_id is not None:
                if (isinstance(requested_company_id, bool)
                        or not isinstance(requested_company_id, (str, int))):
                    return self.send_json({"ok": False, "error": "invalid_request"}, 400)
                requested_company_id = str(requested_company_id)
                if (not requested_company_id.strip() or len(requested_company_id) > 256
                        or any(ord(char) < 32 or ord(char) == 127
                               for char in requested_company_id)):
                    return self.send_json({"ok": False, "error": "invalid_request"}, 400)
            try:
                result = database.assign_store_company(company_store_id, requested_company_id)
            except Exception:
                return self.send_json({"ok": False}, 503)
            if result == "store_not_found":
                return self.send_json({"ok": False, "error": "store_not_found"}, 404)
            if result == "company_not_found":
                return self.send_json({"ok": False, "error": "company_not_found"}, 404)
            return self.send_json({"ok": True, "storeId": company_store_id,
                                   "companyId": requested_company_id})
        store_route = _admin_store_campaign_route(path, include_campaign=True)
        if not store_route:
            return self.send_json({"ok": False, "error": "not found"}, 404)
        if not self.require_admin_session(): return
        if not self.require_same_origin(): return
        if not database.database_enabled():
            return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
        store_id, campaign_id = store_route
        if campaign_id == "default":
            return self.send_json({"ok": False, "error": "not_found"}, 404)
        campaign, error_status = self.read_admin_campaign_payload()
        if error_status:
            return self.send_json({"ok": False, "error": "invalid_request"}, error_status)
        try:
            updated = database.update_store_campaign(store_id, campaign_id, campaign)
        except database.StoreCampaignConflictError:
            return self.send_json({"ok": False, "error": "store_campaign_conflict"}, 409)
        except Exception:
            return self.send_json({"ok": False}, 503)
        if updated is None:
            return self.send_json({"ok": False, "error": "not_found"}, 404)
        return self.send_json({"campaign": _admin_campaign_json(updated)})

    def do_DELETE(self):
        path = urlparse(self.path).path
        prefix = "/api/coupons/"
        if path.startswith(prefix):
            if not self.require_admin_session(): return
            if not self.require_same_origin(): return
            coupon_id = path[len(prefix):]
            if not coupon_id or "/" in coupon_id:
                return self.send_json({"error": "coupon not found"}, 404)
            try:
                if not delete_draft_coupon(coupon_id):
                    return self.send_json({"error": "draft coupon not found"}, 404)
                return self.send_json({"ok": True})
            except Exception:
                return self.send_json({"ok": False}, 500)
        return self.send_json({"error": "not found"}, 404)
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
        if path == "/api/admin/login": return self.handle_admin_login()
        if path == "/api/admin/logout": return self.handle_admin_logout()
        if path == "/api/admin/companies":
            if not self.require_admin_session(): return
            if not self.require_same_origin(): return
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
            raw_payload, error_status = self.read_admin_json_payload()
            if error_status:
                return self.send_json({"ok": False, "error": "invalid_request"}, error_status)
            try:
                company = _validate_admin_company(raw_payload)
            except (TypeError, ValueError):
                return self.send_json({"ok": False, "error": "invalid_request"}, 400)
            try:
                created = database.create_company(
                    "company-" + uuid.uuid4().hex,
                    company["name"], company["code"],
                    company["business_type"], company["status"],
                )
            except database.CompanyCodeConflictError:
                return self.send_json({"ok": False, "error": "company_code_conflict"}, 409)
            except Exception:
                return self.send_json({"ok": False}, 503)
            return self.send_json({"company": {
                "id": created["id"], "name": created["name"], "code": created["code"],
                "businessType": created["business_type"], "status": created["status"],
            }}, 201)
        store_route = _admin_store_campaign_route(path)
        if store_route:
            if not self.require_admin_session(): return
            if not self.require_same_origin(): return
            idempotency_key, valid_key = _read_idempotency_key(self.headers)
            if not valid_key:
                return self.send_json({"ok": False, "error": "invalid_idempotency_key"}, 400)
            if not database.database_enabled():
                return self.send_json({"ok": False, "error": "admin_data_unavailable"}, 503)
            store_id, _ = store_route
            campaign, error_status = self.read_admin_campaign_payload()
            if error_status:
                return self.send_json({"ok": False, "error": "invalid_request"}, error_status)
            campaign_id = "campaign-" + uuid.uuid4().hex
            ad_id = "ad-" + uuid.uuid4().hex
            try:
                if idempotency_key is None:
                    created = database.create_store_campaign(
                        store_id, campaign_id, ad_id, campaign
                    )
                else:
                    created = database.create_store_campaign_idempotent(
                        store_id, campaign_id, ad_id, campaign, idempotency_key
                    )
            except database.StoreCampaignIdempotencyMismatchError:
                return self.send_json(
                    {"ok": False, "error": "idempotency_mismatch"}, 409
                )
            except database.StoreCampaignConflictError:
                return self.send_json({"ok": False, "error": "store_campaign_conflict"}, 409)
            except database.StoreInactiveError:
                return self.send_json({"ok": False, "error": "store_inactive"}, 409)
            except Exception:
                return self.send_json({"ok": False}, 503)
            if created is None:
                return self.send_json({"ok": False, "error": "not_found"}, 404)
            status = 201
            if idempotency_key is not None and (
                created["campaign_id"] != campaign_id or created["ad_id"] != ad_id
            ):
                status = 200
            return self.send_json({"campaign": _admin_campaign_json(created)}, status)
        if path == "/api/coupon_event":
            try:
                payload = json.loads(self.read_body().decode("utf-8"))
                record_coupon_event(payload)
                return self.send_json({"ok": True})
            except database.StoreInactiveError:
                return self.send_json({"ok": False, "error": "store_inactive"}, 409)
            except database.StoreMismatchError:
                return self.send_json({"ok": False, "error": "store_mismatch"}, 409)
            except LookupError as e: return self.send_json({"error": str(e)}, 404)
            except Exception as e: return self.send_json({"error": str(e)}, 400)
        if path == "/api/event":
            try:
                body = self.read_body(); payload = json.loads(body.decode("utf-8"))
                event_type = payload.get("type")
                if event_type not in {"impression", "click"}: return self.send_json({"error":"invalid event"}, 400)
                has_store_context = "storeCode" in payload or "campaignId" in payload
                if has_store_context:
                    store_code = payload.get("storeCode")
                    campaign_id = payload.get("campaignId")
                    ad_id = payload.get("adId")
                    if (not isinstance(store_code, str)
                            or len(store_code) > PUBLIC_CODE_MAX_LENGTH
                            or not PUBLIC_CODE_PATTERN.fullmatch(store_code)
                            or not isinstance(campaign_id, str) or not campaign_id or len(campaign_id) > 512
                            or not isinstance(ad_id, str) or not ad_id or len(ad_id) > 512):
                        return self.send_json({"error":"invalid event"}, 400)
                    if not database.database_enabled():
                        return self.send_json({"error":"event unavailable"}, 503)
                    try:
                        recorded = database.record_store_ad_event(
                            event_type, store_code, campaign_id, ad_id,
                            delivery_date=datetime.now(ZoneInfo("Asia/Tokyo")).date(),
                        )
                    except database.StoreInactiveError:
                        return self.send_json({"ok": False, "error": "store_inactive"}, 409)
                    except Exception:
                        return self.send_json({"error":"event unavailable"}, 503)
                    if not recorded:
                        return self.send_json({"error":"invalid event"}, 400)
                    return self.send_json({"ok": True})
                try:
                    record_event(event_type, payload.get("store", ""), payload.get("adId", "main"))
                except database.StoreInactiveError:
                    return self.send_json({"ok": False, "error": "store_inactive"}, 409)
                return self.send_json({"ok": True})
            except Exception as e: return self.send_json({"error": str(e)}, 400)
        if path == "/api/state":
            if not self.require_admin_session(): return
            if not self.require_same_origin(): return
            try:
                body = self.read_body(); data = json.loads(body.decode("utf-8")); save_state(data)
                return self.send_json({"ok": True})
            except database.StoreInactiveError:
                return self.send_json({"ok": False, "error": "store_inactive"}, 409)
            except Exception: return self.send_json({"ok": False}, 400)
        if path == "/api/upload":
            if not self.require_admin_session(): return
            if not self.require_same_origin(): return
            try:
                try:
                    request_length = int(self.headers.get("Content-Length", "0"))
                except (TypeError, ValueError):
                    return self.send_json({"ok": False, "error": "invalid_request"}, 400)
                if request_length <= 0:
                    return self.send_json({"ok": False, "error": "invalid_request"}, 400)
                if request_length > MAX_BYTES:
                    return self.send_json({"ok": False, "error": "file_too_large"}, 413)
                body = self.read_body(); ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype: return self.send_json({"error":"multipart/form-data required"}, 400)
                header_blob = ("Content-Type: " + ctype + "\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8")
                msg = BytesParser(policy=default).parsebytes(header_blob + body)
                part = next((p for p in msg.iter_parts() if p.get_param("name", header="Content-Disposition") == "file"), None)
                if part is None: return self.send_json({"error":"file not found"}, 400)
                ext = os.path.splitext(part.get_filename() or "")[1].lower()
                if ext not in ALLOWED: return self.send_json({"error":"gif/jpg/png/webp only"}, 400)
                content = part.get_payload(decode=True) or b""
                if len(content) > MAX_BYTES:
                    return self.send_json({"ok": False, "error": "file_too_large"}, 413)
                detected_ext, content_type = _detect_upload_image(content)
                compatible_extensions = {detected_ext}
                if detected_ext == ".jpg":
                    compatible_extensions.add(".jpeg")
                if ext not in compatible_extensions:
                    return self.send_json({"ok": False, "error": "unsupported_image"}, 400)
            except ValueError:
                return self.send_json({"ok": False, "error": "invalid_image"}, 400)
            except Exception:
                return self.send_json({"ok": False, "error": "invalid_request"}, 400)
            try:
                image_url = _store_uploaded_image(content, detected_ext, content_type)
                return self.send_json({"url": image_url})
            except R2StorageConfigurationError:
                logging.error("Image storage unavailable: R2 configuration is missing or invalid")
                return self.send_json({"ok": False, "error": "image_storage_unavailable"}, 503)
            except Exception as exc:
                logging.error("Image storage upload failed (%s)", type(exc).__name__)
                return self.send_json({"ok": False, "error": "image_storage_unavailable"}, 503)
        return self.send_json({"error":"not found"}, 404)

def _json_performance_trend(events, spec):
    jst = ZoneInfo("Asia/Tokyo")
    counts = {}
    for event in events:
        if event.get("type") not in {"impression", "click"}:
            continue
        event_at = event.get("at", "")
        if not event_at:
            continue
        try:
            parsed = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=jst)
        local = parsed.astimezone(jst)
        if not (spec["start"] <= local < spec["end"]):
            continue
        if spec["granularity"] == "hour":
            bucket = local.strftime("%Y-%m-%dT%H")
        elif spec["granularity"] == "day":
            bucket = local.strftime("%Y-%m-%d")
        else:
            bucket = local.strftime("%Y/%m")
        row = counts.setdefault(bucket, {"bucket": bucket, "impressions": 0, "clicks": 0})
        row["impressions" if event["type"] == "impression" else "clicks"] += 1
    return database.shape_performance_trend(spec, list(counts.values()))


def _json_period_store_details(events, spec):
    """Aggregate recent JSON events by known store ID for period rankings."""
    jst = ZoneInfo("Asia/Tokyo")
    grouped = {}
    for event in events:
        event_type = event.get("type")
        if event_type not in {"impression", "click"}:
            continue
        raw_store_id = event.get("storeId", event.get("store_id"))
        if raw_store_id is None:
            continue
        store_id = str(raw_store_id).strip()
        if not store_id:
            continue
        event_at = event.get("at", "")
        if not event_at:
            continue
        try:
            parsed = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=jst)
        local = parsed.astimezone(jst)
        if not (spec["start"] <= local < spec["end"]):
            continue

        store_name = str(event.get("store") or event.get("storeName") or "未設定")
        item = grouped.setdefault(store_id, {
            "storeId": store_id,
            "storeName": store_name,
            "impressions": 0,
            "clicks": 0,
        })
        # Match the PostgreSQL path's deterministic choice if a store was renamed.
        if store_name < item["storeName"]:
            item["storeName"] = store_name
        item["impressions" if event_type == "impression" else "clicks"] += 1

    details = list(grouped.values())
    for item in details:
        impressions = item["impressions"]
        clicks = item["clicks"]
        item["ctr"] = round(clicks / impressions * 100, 2) if impressions else 0
    details.sort(key=lambda item: (
        -item["clicks"], -item["impressions"], item["storeName"], item["storeId"]
    ))
    return details


def _json_period_ad_details(events, spec, ad_names=None):
    """Aggregate selected-period JSON events by explicit ad ID."""
    jst = ZoneInfo("Asia/Tokyo")
    names = ad_names if isinstance(ad_names, dict) else {}
    grouped = {}
    for event in events:
        event_type = event.get("type")
        if event_type not in {"impression", "click"}:
            continue
        raw_ad_id = event.get("adId", event.get("ad_id"))
        if raw_ad_id is None:
            continue
        ad_id = str(raw_ad_id).strip()
        if not ad_id:
            continue
        event_at = event.get("at", "")
        if not event_at:
            continue
        try:
            parsed = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=jst)
        local = parsed.astimezone(jst)
        if not (spec["start"] <= local < spec["end"]):
            continue

        raw_name = names.get(ad_id)
        ad_name = str(raw_name).strip() if raw_name is not None else ""
        if not ad_name:
            ad_name = ad_id
        item = grouped.setdefault(ad_id, {
            "adId": ad_id,
            "adName": ad_name,
            "impressions": 0,
            "clicks": 0,
        })
        item["impressions" if event_type == "impression" else "clicks"] += 1

    details = list(grouped.values())
    for item in details:
        impressions = item["impressions"]
        clicks = item["clicks"]
        item["ctr"] = round(clicks / impressions * 100, 2) if impressions else 0
    details.sort(key=lambda item: (
        -item["clicks"], -item["impressions"], -item["ctr"],
        item["adName"], item["adId"],
    ))
    return details


def build_analytics(events, period=None, ad_names=None):
    trend_spec = database.performance_period_spec(period) if period is not None else None
    previous_trend_spec = database.previous_performance_period_spec(trend_spec) if trend_spec else None
    impressions = [e for e in events if e.get("type") == "impression"]
    clicks = [e for e in events if e.get("type") == "click"]
    def by_store(items):
        out = {}
        for e in items: out[e.get("store", "未設定")] = out.get(e.get("store", "未設定"), 0) + 1
        return out
    days = {}
    months = {}
    for e in events:
        at = e.get("at", "")
        if not at: continue
        try:
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            local = parsed.astimezone(ZoneInfo("Asia/Tokyo"))
            day, month = local.strftime("%Y-%m-%d"), local.strftime("%Y/%m")
        except (TypeError, ValueError):
            continue
        days.setdefault(day, {"impressions":0,"clicks":0})
        months.setdefault(month, {"impressions":0,"clicks":0})
        if e.get("type") not in {"impression", "click"}: continue
        field = "impressions" if e["type"] == "impression" else "clicks"
        days[day][field] += 1
        months[month][field] += 1
    store_names = sorted(set(by_store(impressions)) | set(by_store(clicks)))
    by_ad_data = {}
    by_store_id_data = {}
    for e in events:
        if e.get("type") not in {"impression", "click"}:
            continue
        store_id = e.get("storeId", e.get("store_id"))
        store_name = e.get("store", e.get("storeName", "未設定")) or "未設定"
        store_id_value = str(store_id) if store_id is not None else None
        store_key = ("id", store_id_value) if store_id_value is not None else ("unresolved", store_name)
        store_item = by_store_id_data.setdefault(store_key, {
            "storeId": store_id_value, "storeName": store_name,
            "impressions": 0, "clicks": 0,
        })
        store_item["impressions" if e["type"] == "impression" else "clicks"] += 1
        ad_id = e.get("adId", "main")
        item = by_ad_data.setdefault(ad_id, {"id": ad_id, "name": ad_id, "impressions": 0, "clicks": 0})
        item["impressions" if e["type"] == "impression" else "clicks"] += 1

    for item in by_ad_data.values():
        item["ctr"] = round(item["clicks"] / item["impressions"] * 100, 2) if item["impressions"] else 0

    by_store_details = []
    for item in by_store_id_data.values():
        item["ctr"] = round(item["clicks"] / item["impressions"] * 100, 2) if item["impressions"] else 0
        by_store_details.append(item)
    by_store_details.sort(key=lambda item: (item["storeName"], item["storeId"] or ""))

    result = {
        "impressions": len(impressions), "clicks": len(clicks),
        "ctr": round((len(clicks)/len(impressions)*100), 2) if impressions else 0,
        "byStore": {
            k: {
                "impressions": by_store(impressions).get(k, 0),
                "clicks": by_store(clicks).get(k, 0),
                "ctr": round(
                    by_store(clicks).get(k, 0) / by_store(impressions).get(k, 0) * 100, 2
                ) if by_store(impressions).get(k, 0) else 0,
            }
            for k in store_names
        },
        "byStoreDetails": by_store_details,
        "byAd": list(by_ad_data.values()),
        "daily": [{"date":k, **days[k], "ctr":round(days[k]["clicks"]/days[k]["impressions"]*100,2) if days[k]["impressions"] else 0} for k in sorted(days)],
        "monthly": [{"month":k, **months[k], "ctr":round(months[k]["clicks"]/months[k]["impressions"]*100,2) if months[k]["impressions"] else 0} for k in sorted(months)]
    }
    if trend_spec:
        result["performanceTrend"] = _json_performance_trend(events, trend_spec)
        result["previousPerformanceTrend"] = _json_performance_trend(events, previous_trend_spec)
        result["periodStoreDetails"] = _json_period_store_details(events, trend_spec)
        result["periodAdDetails"] = _json_period_ad_details(events, trend_spec, ad_names)
    return result

if __name__ == "__main__":
    if database.database_enabled():
        # The existing migration is guarded in database.import_legacy_state,
        # so it only imports data.json while PostgreSQL is still empty.
        migrate_to_postgres.main()
    print("Wi-Fi MEDIA サーバーを起動しています")
    print("管理画面: http://127.0.0.1:%d/admin" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
