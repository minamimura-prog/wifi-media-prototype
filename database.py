"""PostgreSQL persistence for the prototype's existing JSON-shaped state API."""

from datetime import datetime, timezone
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
            conn.execute(
                "INSERT INTO store_settings (id, store_id, settings) VALUES ('global', NULL, %s) "
                "ON CONFLICT (id) DO UPDATE SET settings=EXCLUDED.settings, updated_at=now()",
                (Jsonb({"design": design, "legacy_history": data.get("history", [])}),),
            )


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
    return True
