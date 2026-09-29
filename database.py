"""PostgreSQL persistence for the prototype's existing JSON-shaped state API."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

BASE = Path(__file__).resolve().parent
SCHEMA = BASE / "db_schema.sql"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_pool = None

ADMIN_LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
ADMIN_LOGIN_MAX_FAILURES = 10
ADMIN_LOGIN_LOCK_DURATION = timedelta(minutes=15)


def database_enabled():
    return bool(DATABASE_URL)


def pool():
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required to use PostgreSQL")
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _pool.open(wait=True)
    return _pool


def ensure_schema():
    """Create/update the initial schema from db_schema.sql (safe to repeat)."""
    ddl = SCHEMA.read_text(encoding="utf-8")
    with pool().connection() as conn:
        conn.execute(ddl)


def _admin_session_token_hash(raw_token):
    """Hash a raw, high-entropy cookie token once before any database use."""
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("raw_token must be a non-empty string")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _require_postgres_for_admin_sessions():
    if not database_enabled():
        raise RuntimeError("Admin sessions require PostgreSQL; JSON fallback is not supported")


def _utc_aware_datetime(value, name):
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def create_admin_session(raw_token, expires_at):
    """Store only SHA-256(raw_token); created_at uses PostgreSQL's timestamptz clock."""
    _require_postgres_for_admin_sessions()
    token_hash = _admin_session_token_hash(raw_token)
    expires_at_utc = _utc_aware_datetime(expires_at, "expires_at")
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token_hash, expires_at) VALUES (%s, %s)",
            (token_hash, expires_at_utc),
        )


def is_admin_session_valid(raw_token):
    """Return whether SHA-256(raw_token) identifies a session not yet expired."""
    _require_postgres_for_admin_sessions()
    token_hash = _admin_session_token_hash(raw_token)
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT expires_at > CURRENT_TIMESTAMP AS is_valid "
            "FROM admin_sessions WHERE token_hash = %s",
            (token_hash,),
        ).fetchone()
    return bool(row and row["is_valid"])


def revoke_admin_session(raw_token):
    """Delete the session identified by SHA-256(raw_token); never log the token."""
    _require_postgres_for_admin_sessions()
    token_hash = _admin_session_token_hash(raw_token)
    with pool().connection() as conn:
        result = conn.execute(
            "DELETE FROM admin_sessions WHERE token_hash = %s",
            (token_hash,),
        )
    return result.rowcount > 0


def cleanup_expired_admin_sessions():
    """Delete expired sessions using the PostgreSQL server's current timestamp."""
    _require_postgres_for_admin_sessions()
    with pool().connection() as conn:
        result = conn.execute(
            "DELETE FROM admin_sessions WHERE expires_at <= CURRENT_TIMESTAMP"
        )
    return result.rowcount



def _require_postgres_for_admin_login_limits():
    if not database_enabled():
        raise RuntimeError("Admin login limits require PostgreSQL; JSON fallback is not supported")


def is_admin_login_ip_locked(ip_address):
    """Return whether a validated IP address currently has an active login lock."""
    _require_postgres_for_admin_login_limits()
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT locked_until > CURRENT_TIMESTAMP AS is_locked "
            "FROM admin_login_limits WHERE ip_address = %s::inet",
            (ip_address,),
        ).fetchone()
    return bool(row and row["is_locked"])


def record_admin_login_failure(ip_address):
    """Atomically record a failure and return its count and current lock state."""
    _require_postgres_for_admin_login_limits()
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO admin_login_limits "
            "(ip_address, failure_count, window_started_at, locked_until, updated_at) "
            "VALUES (%s::inet, 1, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP) "
            "ON CONFLICT (ip_address) DO UPDATE SET "
            "failure_count = CASE "
            "WHEN admin_login_limits.locked_until > CURRENT_TIMESTAMP "
            "THEN admin_login_limits.failure_count "
            "WHEN admin_login_limits.window_started_at <= CURRENT_TIMESTAMP - %s "
            "THEN 1 ELSE admin_login_limits.failure_count + 1 END, "
            "window_started_at = CASE "
            "WHEN admin_login_limits.locked_until > CURRENT_TIMESTAMP "
            "THEN admin_login_limits.window_started_at "
            "WHEN admin_login_limits.window_started_at <= CURRENT_TIMESTAMP - %s "
            "THEN CURRENT_TIMESTAMP ELSE admin_login_limits.window_started_at END, "
            "locked_until = CASE "
            "WHEN admin_login_limits.locked_until > CURRENT_TIMESTAMP "
            "THEN admin_login_limits.locked_until "
            "WHEN admin_login_limits.window_started_at <= CURRENT_TIMESTAMP - %s "
            "THEN CASE WHEN 1 >= %s THEN CURRENT_TIMESTAMP + %s ELSE NULL END "
            "WHEN admin_login_limits.failure_count + 1 >= %s "
            "THEN CURRENT_TIMESTAMP + %s ELSE NULL END, "
            "updated_at = CURRENT_TIMESTAMP "
            "RETURNING failure_count, locked_until > CURRENT_TIMESTAMP AS is_locked",
            (
                ip_address,
                ADMIN_LOGIN_FAILURE_WINDOW,
                ADMIN_LOGIN_FAILURE_WINDOW,
                ADMIN_LOGIN_FAILURE_WINDOW,
                ADMIN_LOGIN_MAX_FAILURES,
                ADMIN_LOGIN_LOCK_DURATION,
                ADMIN_LOGIN_MAX_FAILURES,
                ADMIN_LOGIN_LOCK_DURATION,
            ),
        ).fetchone()
    return {
        "failure_count": row["failure_count"],
        "is_locked": bool(row["is_locked"]),
    }


def reset_admin_login_failures(ip_address):
    """Remove the supplied IP's login-failure and lock state after success."""
    _require_postgres_for_admin_login_limits()
    with pool().connection() as conn:
        result = conn.execute(
            "DELETE FROM admin_login_limits WHERE ip_address = %s::inet",
            (ip_address,),
        )
    return result.rowcount > 0


def cleanup_admin_login_limits():
    """Delete inactive rows after their failure window and any lock have expired."""
    _require_postgres_for_admin_login_limits()
    with pool().connection() as conn:
        result = conn.execute(
            "DELETE FROM admin_login_limits "
            "WHERE updated_at <= CURRENT_TIMESTAMP - %s "
            "AND (locked_until IS NULL OR locked_until <= CURRENT_TIMESTAMP)",
            (ADMIN_LOGIN_FAILURE_WINDOW,),
        )
    return result.rowcount


def _date(value):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value[:10]


def _store_ids(conn):
    rows = conn.execute("SELECT id, name FROM stores").fetchall()
    return {row["name"]: row["id"] for row in rows}


def load_state():
    with pool().connection() as conn:
        stores = conn.execute(
            "SELECT id, name, store_type, wifi, monthly_users, legacy_clicks, status "
            "FROM stores ORDER BY created_at, id"
        ).fetchall()
        ad = conn.execute(
            "SELECT a.id, a.store_id, a.title, a.body, a.landing_url, a.media_url, "
            "a.starts_on, a.ends_on, a.published, s.name AS store_name "
            "FROM ads a LEFT JOIN stores s ON s.id = a.store_id "
            "ORDER BY a.updated_at DESC, a.id LIMIT 1"
        ).fetchone()
        global_settings = conn.execute(
            "SELECT settings FROM store_settings WHERE id = 'global'"
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, store_name, ad_id, occurred_at "
            "FROM ad_events ORDER BY occurred_at DESC, id DESC LIMIT 10000"
        ).fetchall()
        coupons = conn.execute(
            "SELECT id, ad_id, store_id, title, code, description, discount_type, discount_value, "
            "terms, starts_on, ends_on, status FROM coupons WHERE deleted_at IS NULL "
            "ORDER BY created_at, id"
        ).fetchall()
        coupon_events = conn.execute(
            "SELECT coupon_id, coupon_code, event_type, store_name, ad_id, occurred_at "
            "FROM coupon_events ORDER BY occurred_at DESC, id DESC LIMIT 10000"
        ).fetchall()

    config = global_settings["settings"] if global_settings else {}
    if isinstance(config, str):
        config = json.loads(config)
    ad_data = {
        "id": ad["id"], "store": ad["store_name"] or "未設定",
        "title": ad["title"], "body": ad["body"], "media": ad["media_url"],
        "link": ad["landing_url"], "start": _date(ad["starts_on"]),
        "end": _date(ad["ends_on"]), "published": ad["published"],
    } if ad else {}
    return {
        "design": config.get("design", {}),
        "ad": ad_data,
        "stores": [{
            "id": row["id"], "name": row["name"], "type": row["store_type"],
            "wifi": row["wifi"], "users": row["monthly_users"],
            "clicks": row["legacy_clicks"], "status": row["status"],
        } for row in stores],
        "history": config.get("legacy_history", []),
        "events": [{
            "type": row["event_type"], "store": row["store_name"],
            "adId": row["ad_id"], "at": row["occurred_at"].astimezone(timezone.utc).isoformat(),
        } for row in reversed(events)],
        "coupons": [{
            "id": row["id"], "adId": row["ad_id"], "storeId": row["store_id"],
            "title": row["title"], "code": row["code"], "description": row["description"],
            "discountType": row["discount_type"], "discountValue": row["discount_value"],
            "terms": row["terms"], "start": _date(row["starts_on"]),
            "end": _date(row["ends_on"]), "status": row["status"],
        } for row in coupons],
        "coupon_events": [{
            "couponId": row["coupon_id"], "couponCode": row["coupon_code"],
            "type": row["event_type"], "store": row["store_name"], "adId": row["ad_id"],
            "at": row["occurred_at"].astimezone(timezone.utc).isoformat(),
        } for row in reversed(coupon_events)],
    }


def save_state(data):
    """Persist the current admin UI state without changing its API contract."""
    design = data.get("design") or {}
    stores = data.get("stores") or []
    ad = data.get("ad") or {}
    with pool().connection() as conn:
        with conn.transaction():
            for store in stores:
                store_id = str(store.get("id") or store.get("name") or "store")
                conn.execute(
                    "INSERT INTO stores (id, name, store_type, wifi, monthly_users, legacy_clicks, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, store_type=EXCLUDED.store_type, "
                    "wifi=EXCLUDED.wifi, monthly_users=EXCLUDED.monthly_users, "
                    "legacy_clicks=EXCLUDED.legacy_clicks, status=EXCLUDED.status, updated_at=now()",
                    (store_id, store.get("name", ""), store.get("type", ""), store.get("wifi", ""),
                     int(store.get("users") or 0), int(store.get("clicks") or 0), store.get("status", "稼働中")),
                )

            store_ids = _store_ids(conn)
            ad_id = str(ad.get("id") or "main")
            store_id = store_ids.get(ad.get("store"))
            conn.execute(
                "INSERT INTO ads (id, store_id, title, body, landing_url, media_url, starts_on, ends_on, published) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET store_id=EXCLUDED.store_id, title=EXCLUDED.title, "
                "body=EXCLUDED.body, landing_url=EXCLUDED.landing_url, media_url=EXCLUDED.media_url, "
                "starts_on=EXCLUDED.starts_on, ends_on=EXCLUDED.ends_on, published=EXCLUDED.published, updated_at=now()",
                (ad_id, store_id, ad.get("title", ""), ad.get("body", ""), ad.get("link", ""),
                 ad.get("media", ""), _date(ad.get("start")), _date(ad.get("end")), bool(ad.get("published", False))),
            )
            campaign_id = "default"
            conn.execute(
                "INSERT INTO campaigns (id, ad_id, name, starts_on, ends_on, status) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET ad_id=EXCLUDED.ad_id, name=EXCLUDED.name, "
                "starts_on=EXCLUDED.starts_on, ends_on=EXCLUDED.ends_on, status=EXCLUDED.status, updated_at=now()",
                (campaign_id, ad_id, ad.get("title", ""), _date(ad.get("start")), _date(ad.get("end")),
                 "active" if ad.get("published") else "draft"),
            )
            conn.execute("DELETE FROM campaign_stores WHERE campaign_id = %s", (campaign_id,))
            target_id = store_id
            if target_id:
                conn.execute(
                    "INSERT INTO campaign_stores (campaign_id, store_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (campaign_id, target_id),
                )
            for coupon in data.get("coupons", []):
                coupon_id = str(coupon.get("id") or "")
                if not coupon_id:
                    continue
                coupon_store_id = coupon.get("storeId") or store_ids.get(coupon.get("store"))
                conn.execute(
                    "INSERT INTO coupons (id, ad_id, store_id, title, code, description, discount_type, "
                    "discount_value, terms, starts_on, ends_on, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET ad_id=EXCLUDED.ad_id, store_id=EXCLUDED.store_id, "
                    "title=EXCLUDED.title, code=EXCLUDED.code, description=EXCLUDED.description, "
                    "discount_type=EXCLUDED.discount_type, discount_value=EXCLUDED.discount_value, "
                    "terms=EXCLUDED.terms, starts_on=EXCLUDED.starts_on, ends_on=EXCLUDED.ends_on, "
                    "status=EXCLUDED.status, updated_at=now() WHERE coupons.deleted_at IS NULL",
                    (coupon_id, coupon.get("adId") or ad_id, coupon_store_id, coupon.get("title", ""),
                     coupon.get("code", ""), coupon.get("description", ""),
                     coupon.get("discountType", "text"), str(coupon.get("discountValue", "")),
                     coupon.get("terms", ""), _date(coupon.get("start")), _date(coupon.get("end")),
                     coupon.get("status", "draft")),
                )
            conn.execute(
                "INSERT INTO store_settings (id, store_id, settings) VALUES ('global', NULL, %s) "
                "ON CONFLICT (id) DO UPDATE SET settings=EXCLUDED.settings, updated_at=now()",
                (Jsonb({"design": design, "legacy_history": data.get("history", [])}),),
            )


def delete_draft_coupon(coupon_id):
    """Delete a draft coupon while retaining its coupon event history."""
    with pool().connection() as conn:
        with conn.transaction():
            result = conn.execute(
                "UPDATE coupons SET deleted_at = now(), updated_at = now() "
                "WHERE id = %s AND status = 'draft' AND deleted_at IS NULL RETURNING id",
                (str(coupon_id),),
            ).fetchone()
            return result is not None


def analytics():
    """Aggregate all ad events using Japan's calendar days and months."""
    with pool().connection() as conn:
        totals = conn.execute(
            "SELECT count(*) FILTER (WHERE event_type = 'impression') AS impressions, "
            "count(*) FILTER (WHERE event_type = 'click') AS clicks FROM ad_events"
        ).fetchone()
        stores = conn.execute(
            "SELECT store_name, count(*) FILTER (WHERE event_type = 'impression') AS impressions, "
            "count(*) FILTER (WHERE event_type = 'click') AS clicks FROM ad_events "
            "GROUP BY store_name ORDER BY store_name"
        ).fetchall()
        ads = conn.execute(
            "SELECT e.ad_id, COALESCE(NULLIF(a.title, ''), e.ad_id) AS ad_name, "
            "count(*) FILTER (WHERE e.event_type = 'impression') AS impressions, "
            "count(*) FILTER (WHERE e.event_type = 'click') AS clicks "
            "FROM ad_events e LEFT JOIN ads a ON a.id = e.ad_id "
            "GROUP BY e.ad_id, a.title ORDER BY ad_name, e.ad_id"
        ).fetchall()
        periods = conn.execute(
            "SELECT to_char(occurred_at AT TIME ZONE 'Asia/Tokyo', 'YYYY-MM-DD') AS date, "
            "count(*) FILTER (WHERE event_type = 'impression') AS impressions, "
            "count(*) FILTER (WHERE event_type = 'click') AS clicks FROM ad_events "
            "GROUP BY date ORDER BY date"
        ).fetchall()
        months = conn.execute(
            "SELECT to_char(occurred_at AT TIME ZONE 'Asia/Tokyo', 'YYYY/MM') AS month, "
            "count(*) FILTER (WHERE event_type = 'impression') AS impressions, "
            "count(*) FILTER (WHERE event_type = 'click') AS clicks FROM ad_events "
            "GROUP BY month ORDER BY month"
        ).fetchall()
    impressions, clicks = totals["impressions"], totals["clicks"]
    def shape(rows, key):
        return [{key: row[key], "impressions": row["impressions"], "clicks": row["clicks"],
                 "ctr": round(row["clicks"] / row["impressions"] * 100, 2) if row["impressions"] else 0}
                for row in rows]
    return {
        "impressions": impressions, "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0,
        "byStore": {
            row["store_name"]: {
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                "ctr": round(row["clicks"] / row["impressions"] * 100, 2) if row["impressions"] else 0,
            }
            for row in stores
        },
        "byAd": [
            {
                "id": row["ad_id"],
                "name": row["ad_name"],
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                "ctr": round(row["clicks"] / row["impressions"] * 100, 2) if row["impressions"] else 0,
            }
            for row in ads
        ],
        "daily": shape(periods, "date"), "monthly": shape(months, "month"),
    }


def record_event(event_type, store_name, ad_id="main", occurred_at=None):
    occurred_at = occurred_at or datetime.now(timezone.utc)
    with pool().connection() as conn:
        with conn.transaction():
            # The browser's store value may be absent or stale. Prefer the
            # store currently configured for this ad, using the submitted
            # value only when this ad has no configured store.
            ad = conn.execute(
                "SELECT s.id, s.name FROM ads a LEFT JOIN stores s ON s.id = a.store_id "
                "WHERE a.id = %s",
                (ad_id,),
            ).fetchone()
            if ad and ad["name"]:
                store_name = ad["name"]
            elif not store_name or store_name == "未設定":
                store_name = "未設定"
            store = conn.execute("SELECT id FROM stores WHERE name = %s ORDER BY id LIMIT 1", (store_name,)).fetchone()
            campaign = conn.execute("SELECT id FROM campaigns WHERE ad_id = %s ORDER BY id LIMIT 1", (ad_id,)).fetchone()
            conn.execute(
                "INSERT INTO ad_events (event_type, store_id, store_name, ad_id, campaign_id, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (event_type, store["id"] if store else None, store_name or "未設定", ad_id,
                 campaign["id"] if campaign else None, occurred_at),
            )


def record_coupon_event(event):
    """Persist coupon history separately from impression/click analytics."""
    occurred_at = event.get("at")
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    occurred_at = occurred_at or datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    with pool().connection() as conn:
        coupon = conn.execute(
            "SELECT store_id FROM coupons WHERE id = %s", (event.get("couponId"),)
        ).fetchone() if event.get("couponId") else None
        store_id = event.get("storeId") or (coupon["store_id"] if coupon else None)
        if not store_id and event.get("store"):
            store = conn.execute(
                "SELECT id FROM stores WHERE name = %s ORDER BY id LIMIT 1", (event["store"],)
            ).fetchone()
            store_id = store["id"] if store else None
        conn.execute(
            "INSERT INTO coupon_events (coupon_id, coupon_code, event_type, store_id, store_name, ad_id, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (event.get("couponId"), event.get("couponCode", ""), event.get("type", ""), store_id,
             event.get("store", "未設定"), event.get("adId", "main"), occurred_at),
        )


def coupon_analytics():
    """Return coupon-only event totals; ad_events and its CTR stay untouched."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT coupon_id, coupon_code, event_type, count(*) AS total "
            "FROM coupon_events GROUP BY coupon_id, coupon_code, event_type "
            "ORDER BY coupon_code, coupon_id, event_type"
        ).fetchall()
    return build_coupon_analytics(rows)


def build_coupon_analytics(rows):
    by_type = {}
    by_coupon = {}
    total = 0
    for row in rows:
        coupon_id = row.get("coupon_id")
        coupon_code = row.get("coupon_code") or ""
        event_type = row.get("event_type") or "unknown"
        count = int(row.get("total", 0))
        total += count
        by_type[event_type] = by_type.get(event_type, 0) + count
        key = (coupon_id, coupon_code)
        coupon = by_coupon.setdefault(key, {
            "couponId": coupon_id, "couponCode": coupon_code,
            "total": 0, "byType": {},
        })
        coupon["total"] += count
        coupon["byType"][event_type] = coupon["byType"].get(event_type, 0) + count
    return {
        "total": total,
        "byType": by_type,
        "byCoupon": list(by_coupon.values()),
    }


def import_legacy_state(data):
    """One-time import guard: never overwrites a populated PostgreSQL database."""
    with pool().connection() as conn:
        populated = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM stores) OR EXISTS (SELECT 1 FROM ads) AS populated"
        ).fetchone()["populated"]
        if populated:
            return False
    save_state(data)
    for event in data.get("events", []):
        event_type = event.get("type")
        if event_type in {"impression", "click"}:
            event_at = event.get("at")
            try:
                occurred_at = datetime.fromisoformat(event_at.replace("Z", "+00:00")) if event_at else None
            except (TypeError, ValueError):
                occurred_at = None
            if occurred_at is not None and occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            record_event(
                event_type,
                event.get("store", "未設定"),
                event.get("adId", "main"),
                occurred_at=occurred_at,
            )
    for event in data.get("coupon_events", []):
        try:
            record_coupon_event(event)
        except (TypeError, ValueError):
            continue
    return True
