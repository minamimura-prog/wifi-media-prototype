"""PostgreSQL persistence for the prototype's existing JSON-shaped state API."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import re
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
_PUBLIC_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class StoreCampaignConflictError(Exception):
    """Raised when a store already has a publicly deliverable campaign in the same period."""


class StoreCampaignIdempotencyMismatchError(Exception):
    """Raised when an idempotency key is reused for another store or payload."""


class CompanyCodeConflictError(Exception):
    """Raised when a company code is already assigned to another company."""


_STORE_CAMPAIGN_IDEMPOTENCY_MAX_KEY_LENGTH = 255


def _store_campaign_payload_hash(campaign):
    """Hash the validated store-campaign request using stable API field names."""
    starts_on = campaign.get("starts_on")
    ends_on = campaign.get("ends_on")
    canonical = {
        "name": campaign["name"],
        "status": campaign["status"],
        "startsOn": starts_on.isoformat() if starts_on is not None else None,
        "endsOn": ends_on.isoformat() if ends_on is not None else None,
        "title": campaign["title"],
        "body": campaign["body"],
        "landingUrl": campaign["landing_url"],
        "mediaUrl": campaign["media_url"],
        "published": campaign["published"],
    }
    # Preserve hashes created before the mobile image field existed when the
    # field is missing or blank. A real mobile URL is part of request identity.
    media_url_mobile = campaign.get("media_url_mobile")
    if media_url_mobile:
        canonical["mediaUrlMobile"] = media_url_mobile
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _campaign_periods_overlap(left_start, left_end, right_start, right_end):
    """Treat inclusive campaign dates with NULL endpoints as unbounded."""
    return not (
        (left_end is not None and right_start is not None and left_end < right_start)
        or (right_end is not None and left_start is not None and right_end < left_start)
    )


def _ensure_store_campaign_period_available(
    conn, store_id, campaign, exclude_campaign_id=None
):
    if campaign.get("status") != "active" or campaign.get("published") is not True:
        return
    sql = (
        "SELECT c.id, c.starts_on, c.ends_on "
        "FROM campaign_stores cs "
        "JOIN campaigns c ON c.id = cs.campaign_id "
        "JOIN ads a ON a.id = c.ad_id "
        "WHERE cs.store_id = %s AND c.id <> %s "
        "AND c.status = 'active' AND a.published IS TRUE"
    )
    params = [str(store_id), "default"]
    if exclude_campaign_id is not None:
        sql += " AND c.id <> %s"
        params.append(str(exclude_campaign_id))
    existing_campaigns = conn.execute(sql, tuple(params)).fetchall()
    for existing in existing_campaigns:
        if _campaign_periods_overlap(
            existing["starts_on"], existing["ends_on"],
            campaign.get("starts_on"), campaign.get("ends_on"),
        ):
            raise StoreCampaignConflictError("store campaign delivery periods overlap")


def database_enabled():
    return bool(DATABASE_URL)


def public_code_for_store_id(store_id):
    """Return a bounded URL-safe code derived only from a store's internal ID."""
    stable_id = str(store_id or "")
    # Hash every ID into one reserved namespace. Keeping safe IDs verbatim
    # would allow a converted ID to collide with a real safe ID (for example,
    # "ABC" and "store-x414243"). Hashing the UTF-8 hex representation also
    # matches PostgreSQL's built-in md5(text) without requiring extensions.
    digest_input = stable_id.encode("utf-8").hex().encode("ascii")
    return "store-" + hashlib.md5(digest_input).hexdigest()


def ensure_public_codes_in_state(state):
    """Return an in-memory JSON-state copy with unique public codes; never persist it."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")
    enriched = dict(state)
    code_owners = {}
    stores = [dict(store) if isinstance(store, dict) else store for store in (state.get("stores") or [])]

    # Preserve existing valid codes. Refuse collisions instead of assigning a
    # suffix based on iteration order, which would make codes order-dependent.
    for store in stores:
        if not isinstance(store, dict):
            continue
        current = store.get("publicCode")
        if isinstance(current, str) and _PUBLIC_CODE_RE.fullmatch(current):
            store_id = str(store.get("id") or "")
            owner = code_owners.get(current)
            if owner is not None and owner != store_id:
                raise ValueError("duplicate existing publicCode values")
            code_owners[current] = store_id
        elif current is not None:
            store["publicCode"] = None

    for store in stores:
        if not isinstance(store, dict) or store.get("publicCode") is not None:
            continue
        store_id = str(store.get("id") or "")
        candidate = public_code_for_store_id(store_id)
        owner = code_owners.get(candidate)
        if owner is not None and owner != store_id:
            raise ValueError("publicCode hash collision")
        store["publicCode"] = candidate
        code_owners[candidate] = store_id
    enriched["stores"] = stores
    return enriched


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


def _unique_public_code(conn, store_id):
    stable_id = str(store_id or "")
    candidate = public_code_for_store_id(stable_id)
    if conn.execute(
        "SELECT 1 FROM stores WHERE public_code = %s AND id <> %s LIMIT 1",
        (candidate, stable_id),
    ).fetchone():
        raise ValueError("publicCode hash collision")
    return candidate


def get_store_by_public_code(public_code):
    """Look up one store by its exact public URL code, returning None if absent/invalid."""
    if not isinstance(public_code, str) or not _PUBLIC_CODE_RE.fullmatch(public_code):
        return None
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT id, company_id, public_code, name, store_type, wifi, monthly_users, legacy_clicks, status "
            "FROM stores WHERE public_code = %s",
            (public_code,),
        ).fetchone()
    return dict(row) if row else None


def get_store_by_id(store_id):
    """Look up one store by its internal ID, returning None if absent."""
    if store_id is None:
        return None
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT id, company_id, public_code, name, store_type, wifi, monthly_users, legacy_clicks, status "
            "FROM stores WHERE id = %s",
            (str(store_id),),
        ).fetchone()
    return dict(row) if row else None


def get_campaigns_for_store(store_id):
    """Return store-delivery candidates, excluding the reserved global campaign."""
    if store_id is None:
        return []
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT c.id AS campaign_id, c.ad_id, c.name AS campaign_name, "
            "c.starts_on, c.ends_on, c.status AS campaign_status, "
            "a.title, a.body, a.landing_url, a.media_url, "
            "COALESCE(a.media_url_mobile, '') AS media_url_mobile, a.published "
            "FROM campaign_stores cs "
            "JOIN campaigns c ON c.id = cs.campaign_id "
            "JOIN ads a ON a.id = c.ad_id "
            "WHERE cs.store_id = %s AND c.id <> %s "
            "ORDER BY c.created_at, c.id",
            (str(store_id), "default"),
        ).fetchall()
    return [dict(row) for row in rows]


def list_admin_stores():
    """Return the minimal store fields used by authenticated admin tools."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, company_id, name, public_code FROM stores ORDER BY created_at, id"
        ).fetchall()
    return [dict(row) for row in rows]


def list_companies():
    """Return company records for authenticated administration tools."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, name, code, business_type, status, created_at, updated_at "
            "FROM companies ORDER BY created_at, id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_company(company_id):
    """Return one company by its internal ID, or None if it does not exist."""
    if company_id is None:
        return None
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT id, name, code, business_type, status, created_at, updated_at "
            "FROM companies WHERE id = %s",
            (str(company_id),),
        ).fetchone()
    return dict(row) if row else None


def save_company(company):
    """Insert or update one company record; store assignments are not changed."""
    if not isinstance(company, dict):
        raise ValueError("company must be an object")
    company_id = company.get("id")
    name = company.get("name")
    code = company.get("code")
    business_type = company.get("business_type", "")
    status = company.get("status", "active")
    if not all(isinstance(value, str) and value.strip() for value in (company_id, name, code)):
        raise ValueError("company id, name, and code are required")
    if not isinstance(business_type, str) or not isinstance(status, str) or not status.strip():
        raise ValueError("invalid company fields")
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO companies (id, name, code, business_type, status) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, code=EXCLUDED.code, "
            "business_type=EXCLUDED.business_type, status=EXCLUDED.status, updated_at=now() "
            "RETURNING id, name, code, business_type, status, created_at, updated_at",
            (company_id.strip(), name.strip(), code.strip(), business_type.strip(), status.strip()),
        ).fetchone()
    return dict(row)


def create_company(company_id, name, code, business_type="", status="active"):
    """Create a company without changing existing company or store records."""
    try:
        with pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO companies (id, name, code, business_type, status) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id, name, code, business_type, status, created_at, updated_at",
                (str(company_id), name, code, business_type, status),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "companies_code_key":
            raise CompanyCodeConflictError from exc
        raise
    return dict(row)


def assign_store_company(store_id, company_id):
    """Assign a store to a company, or explicitly clear the assignment with None."""
    with pool().connection() as conn:
        with conn.transaction():
            store = conn.execute(
                "SELECT id FROM stores WHERE id = %s FOR UPDATE", (str(store_id),)
            ).fetchone()
            if not store:
                return "store_not_found"
            if company_id is not None:
                company = conn.execute(
                    "SELECT id FROM companies WHERE id = %s", (str(company_id),)
                ).fetchone()
                if not company:
                    return "company_not_found"
            conn.execute(
                "UPDATE stores SET company_id = %s, updated_at = now() WHERE id = %s",
                (str(company_id) if company_id is not None else None, str(store_id)),
            )
    return "ok"


def get_admin_campaigns_for_store(store_id):
    """Return editable store campaigns, excluding reserved global records."""
    if store_id is None:
        return []
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT c.id AS campaign_id, c.ad_id, c.name AS campaign_name, "
            "c.status AS campaign_status, c.starts_on, c.ends_on, "
            "c.created_at AS campaign_created_at, c.updated_at AS campaign_updated_at, "
            "a.title, a.body, a.landing_url, a.media_url, "
            "COALESCE(a.media_url_mobile, '') AS media_url_mobile, a.published, "
            "a.created_at AS ad_created_at, a.updated_at AS ad_updated_at "
            "FROM campaign_stores cs "
            "JOIN campaigns c ON c.id = cs.campaign_id "
            "JOIN ads a ON a.id = c.ad_id "
            "WHERE cs.store_id = %s AND c.id <> %s AND a.id <> %s "
            "ORDER BY c.created_at, c.id",
            (str(store_id), "default", "main"),
        ).fetchall()
    return [dict(row) for row in rows]


def create_store_campaign(store_id, campaign_id, ad_id, campaign):
    """Create an ad, campaign, and assignment atomically for one store."""
    if campaign_id == "default" or ad_id == "main":
        raise ValueError("reserved identifiers")
    with pool().connection() as conn:
        with conn.transaction():
            store = conn.execute(
                "SELECT id FROM stores WHERE id = %s FOR UPDATE",
                (str(store_id),),
            ).fetchone()
            if not store:
                return None
            _ensure_store_campaign_period_available(conn, store_id, campaign)
            # campaign_stores is the sole delivery assignment. Keep the legacy
            # ads.store_id column NULL rather than maintaining a second mapping.
            conn.execute(
                "INSERT INTO ads "
                "(id, store_id, title, body, landing_url, media_url, media_url_mobile, published) "
                "VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)",
                (ad_id, campaign["title"], campaign["body"], campaign["landing_url"],
                 campaign["media_url"], campaign.get("media_url_mobile") or "",
                 campaign["published"]),
            )
            conn.execute(
                "INSERT INTO campaigns (id, ad_id, name, starts_on, ends_on, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (campaign_id, ad_id, campaign["name"], campaign["starts_on"],
                 campaign["ends_on"], campaign["status"]),
            )
            conn.execute(
                "INSERT INTO campaign_stores (campaign_id, store_id) VALUES (%s, %s)",
                (campaign_id, str(store_id)),
            )
    return _admin_campaign_result(campaign_id, ad_id, campaign)


def create_store_campaign_idempotent(
    store_id, campaign_id, ad_id, campaign, idempotency_key
):
    """Create once per key and return the original IDs for an identical retry.

    The key claim, overlap check, three campaign inserts, and durable result
    record share one transaction. The unique key insert serializes cross-store
    races; the store row lock remains the common lock used by ordinary POST/PUT.
    """
    if (not isinstance(idempotency_key, str) or not idempotency_key
            or len(idempotency_key) > _STORE_CAMPAIGN_IDEMPOTENCY_MAX_KEY_LENGTH
            or any(ord(char) < 33 or ord(char) == 127 for char in idempotency_key)):
        raise ValueError("invalid idempotency key")
    if campaign_id == "default" or ad_id == "main":
        raise ValueError("reserved identifiers")
    payload_hash = _store_campaign_payload_hash(campaign)
    store_id = str(store_id)
    with pool().connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                "SELECT store_id, payload_hash, campaign_id, ad_id "
                "FROM store_campaign_idempotency WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if (str(existing["store_id"]) != store_id
                        or existing["payload_hash"] != payload_hash):
                    raise StoreCampaignIdempotencyMismatchError(
                        "idempotency key was already used for a different request"
                    )
                return _admin_campaign_result(
                    existing["campaign_id"], existing["ad_id"], campaign
                )

            store = conn.execute(
                "SELECT id FROM stores WHERE id = %s FOR UPDATE",
                (store_id,),
            ).fetchone()
            if not store:
                return None

            claimed = conn.execute(
                "INSERT INTO store_campaign_idempotency "
                "(idempotency_key, store_id, payload_hash, campaign_id, ad_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING idempotency_key",
                (idempotency_key, store_id, payload_hash, campaign_id, ad_id),
            ).fetchone()
            if not claimed:
                existing = conn.execute(
                    "SELECT store_id, payload_hash, campaign_id, ad_id "
                    "FROM store_campaign_idempotency WHERE idempotency_key = %s",
                    (idempotency_key,),
                ).fetchone()
                if (not existing or str(existing["store_id"]) != store_id
                        or existing["payload_hash"] != payload_hash):
                    raise StoreCampaignIdempotencyMismatchError(
                        "idempotency key was already used for a different request"
                    )
                return _admin_campaign_result(
                    existing["campaign_id"], existing["ad_id"], campaign
                )

            _ensure_store_campaign_period_available(conn, store_id, campaign)
            # Keep the legacy ads.store_id column NULL: campaign_stores remains
            # the sole source of truth for store delivery assignment.
            conn.execute(
                "INSERT INTO ads "
                "(id, store_id, title, body, landing_url, media_url, media_url_mobile, published) "
                "VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)",
                (ad_id, campaign["title"], campaign["body"],
                 campaign["landing_url"], campaign["media_url"],
                 campaign.get("media_url_mobile") or "", campaign["published"]),
            )
            conn.execute(
                "INSERT INTO campaigns (id, ad_id, name, starts_on, ends_on, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (campaign_id, ad_id, campaign["name"], campaign["starts_on"],
                 campaign["ends_on"], campaign["status"]),
            )
            conn.execute(
                "INSERT INTO campaign_stores (campaign_id, store_id) VALUES (%s, %s)",
                (campaign_id, store_id),
            )
    return _admin_campaign_result(campaign_id, ad_id, campaign)


def update_store_campaign(store_id, campaign_id, campaign):
    """Update only an exclusively-owned, non-global campaign assigned to a store."""
    if campaign_id == "default":
        return None
    with pool().connection() as conn:
        with conn.transaction():
            store = conn.execute(
                "SELECT id FROM stores WHERE id = %s FOR UPDATE",
                (str(store_id),),
            ).fetchone()
            if not store:
                return None
            current = conn.execute(
                "SELECT c.id AS campaign_id, c.ad_id, "
                "COALESCE(a.media_url_mobile, '') AS media_url_mobile "
                "FROM campaign_stores cs "
                "JOIN campaigns c ON c.id = cs.campaign_id "
                "JOIN ads a ON a.id = c.ad_id "
                "WHERE cs.store_id = %s AND c.id = %s "
                "AND c.id <> %s AND a.id <> %s "
                "AND NOT EXISTS (SELECT 1 FROM campaigns other "
                "WHERE other.ad_id = a.id AND other.id <> c.id) "
                "FOR UPDATE OF cs, c, a",
                (str(store_id), str(campaign_id), "default", "main"),
            ).fetchone()
            if not current:
                return None
            _ensure_store_campaign_period_available(
                conn, store_id, campaign, exclude_campaign_id=current["campaign_id"]
            )
            conn.execute(
                "UPDATE campaigns SET name = %s, starts_on = %s, ends_on = %s, "
                "status = %s, updated_at = now() WHERE id = %s",
                (campaign["name"], campaign["starts_on"], campaign["ends_on"],
                 campaign["status"], current["campaign_id"]),
            )
            if "media_url_mobile" in campaign:
                conn.execute(
                    "UPDATE ads SET title = %s, body = %s, landing_url = %s, media_url = %s, "
                    "media_url_mobile = %s, published = %s, updated_at = now() WHERE id = %s",
                    (campaign["title"], campaign["body"], campaign["landing_url"],
                     campaign["media_url"], campaign.get("media_url_mobile") or "",
                     campaign["published"], current["ad_id"]),
                )
            else:
                conn.execute(
                    "UPDATE ads SET title = %s, body = %s, landing_url = %s, media_url = %s, "
                    "published = %s, updated_at = now() WHERE id = %s",
                    (campaign["title"], campaign["body"], campaign["landing_url"],
                     campaign["media_url"], campaign["published"], current["ad_id"]),
                )
            campaign.setdefault("media_url_mobile", current["media_url_mobile"] or "")
    return _admin_campaign_result(current["campaign_id"], current["ad_id"], campaign)


def _admin_campaign_result(campaign_id, ad_id, campaign):
    return {
        "campaign_id": campaign_id,
        "ad_id": ad_id,
        "campaign_name": campaign["name"],
        "campaign_status": campaign["status"],
        "starts_on": campaign["starts_on"],
        "ends_on": campaign["ends_on"],
        "title": campaign["title"],
        "body": campaign["body"],
        "landing_url": campaign["landing_url"],
        "media_url": campaign["media_url"],
        "media_url_mobile": campaign.get("media_url_mobile") or "",
        "published": campaign["published"],
    }


def load_state():
    with pool().connection() as conn:
        stores = conn.execute(
            "SELECT id, company_id, name, store_type, wifi, monthly_users, legacy_clicks, status "
            "FROM stores ORDER BY created_at, id"
        ).fetchall()
        ad = conn.execute(
            "SELECT a.id, a.store_id, a.title, a.body, a.landing_url, a.media_url, "
            "COALESCE(a.media_url_mobile, '') AS media_url_mobile, "
            "a.starts_on, a.ends_on, a.published, s.name AS store_name "
            "FROM campaigns c "
            "JOIN ads a ON a.id = c.ad_id "
            "LEFT JOIN stores s ON s.id = a.store_id "
            "WHERE c.id = %s",
            ("default",),
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
        "mediaMobile": ad["media_url_mobile"] or "",
        "link": ad["landing_url"], "start": _date(ad["starts_on"]),
        "end": _date(ad["ends_on"]), "published": ad["published"],
    } if ad else {}
    return {
        "design": config.get("design", {}),
        "ad": ad_data,
        "stores": [{
            "id": row["id"], "companyId": row["company_id"], "name": row["name"], "type": row["store_type"],
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
                public_code = _unique_public_code(conn, store_id)
                conn.execute(
                    "INSERT INTO stores (id, public_code, name, store_type, wifi, monthly_users, legacy_clicks, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, store_type=EXCLUDED.store_type, "
                    "wifi=EXCLUDED.wifi, monthly_users=EXCLUDED.monthly_users, "
                    "legacy_clicks=EXCLUDED.legacy_clicks, status=EXCLUDED.status, "
                    "public_code=COALESCE(stores.public_code, EXCLUDED.public_code), updated_at=now()",
                    (store_id, public_code, store.get("name", ""), store.get("type", ""), store.get("wifi", ""),
                     int(store.get("users") or 0), int(store.get("clicks") or 0), store.get("status", "稼働中")),
                )

            store_ids = _store_ids(conn)
            ad_id = str(ad.get("id") or "main")
            store_id = store_ids.get(ad.get("store"))
            has_mobile_media = "mediaMobile" in ad
            mobile_media = ad.get("mediaMobile") or ""
            conn.execute(
                "INSERT INTO ads (id, store_id, title, body, landing_url, media_url, media_url_mobile, starts_on, ends_on, published) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET store_id=EXCLUDED.store_id, title=EXCLUDED.title, "
                "body=EXCLUDED.body, landing_url=EXCLUDED.landing_url, media_url=EXCLUDED.media_url, "
                "media_url_mobile=CASE WHEN %s THEN EXCLUDED.media_url_mobile ELSE ads.media_url_mobile END, "
                "starts_on=EXCLUDED.starts_on, ends_on=EXCLUDED.ends_on, published=EXCLUDED.published, updated_at=now()",
                (ad_id, store_id, ad.get("title", ""), ad.get("body", ""), ad.get("link", ""),
                 ad.get("media", ""), mobile_media, _date(ad.get("start")), _date(ad.get("end")),
                 bool(ad.get("published", False)), has_mobile_media),
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
        store_campaign_ads = conn.execute(
            "SELECT e.store_id, e.store_name, e.campaign_id, c.name AS campaign_name, "
            "e.ad_id, a.title AS ad_title, "
            "count(*) FILTER (WHERE e.event_type = 'impression') AS impressions, "
            "count(*) FILTER (WHERE e.event_type = 'click') AS clicks "
            "FROM ad_events e "
            "JOIN campaigns c ON c.id = e.campaign_id "
            "JOIN ads a ON a.id = e.ad_id "
            "WHERE e.campaign_id IS NOT NULL AND e.campaign_id <> 'default' "
            "GROUP BY e.store_id, e.store_name, e.campaign_id, c.name, e.ad_id, a.title "
            "ORDER BY e.store_name, a.title, e.campaign_id, e.ad_id"
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
        "storeCampaignAds": [
            {
                "storeId": row["store_id"],
                "storeName": row["store_name"],
                "campaignId": row["campaign_id"],
                "campaignName": row["campaign_name"],
                "adId": row["ad_id"],
                "adTitle": row["ad_title"],
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                "ctr": round(row["clicks"] / row["impressions"] * 100, 1)
                if row["impressions"] else 0,
            }
            for row in store_campaign_ads
        ],
        "daily": shape(periods, "date"), "monthly": shape(months, "month"),
    }


def dashboard_analytics(scope="all", company_id=None, now=None):
    """Return current Japan-month KPI values for one administrative store scope."""
    if scope not in {"all", "company", "unassigned"}:
        raise ValueError("invalid dashboard analytics scope")
    if scope == "company":
        if not isinstance(company_id, str) or not company_id.strip():
            raise ValueError("company_id is required")
        store_filter = "company_id = %s"
        store_params = (company_id,)
    elif scope == "unassigned":
        if company_id is not None:
            raise ValueError("company_id is not valid for unassigned scope")
        store_filter = "(company_id IS NULL OR btrim(company_id) = '')"
        store_params = ()
    else:
        if company_id is not None:
            raise ValueError("company_id is not valid for all scope")
        store_filter = "TRUE"
        store_params = ()

    as_of = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    as_of_jst = as_of.astimezone(ZoneInfo("Asia/Tokyo"))
    month_start_jst = as_of_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_utc = month_start_jst.astimezone(timezone.utc)
    as_of_utc = as_of_jst.astimezone(timezone.utc)

    query = (
        "WITH scoped_stores AS MATERIALIZED ("
        "SELECT id FROM stores WHERE " + store_filter + ""
        "), store_total AS ("
        "SELECT count(*) AS store_count FROM scoped_stores"
        ") "
        "SELECT store_total.store_count, "
        "count(*) FILTER (WHERE e.event_type = 'impression') AS impressions, "
        "count(*) FILTER (WHERE e.event_type = 'click') AS clicks "
        "FROM store_total LEFT JOIN ad_events e ON "
        "e.store_id IN (SELECT id FROM scoped_stores) "
        "AND e.occurred_at >= %s AND e.occurred_at <= %s "
        "AND e.campaign_id IS NOT NULL AND e.campaign_id <> 'default' "
        "AND EXISTS ("
        "SELECT 1 FROM campaign_stores cs "
        "JOIN campaigns c ON c.id = cs.campaign_id "
        "JOIN ads a ON a.id = c.ad_id "
        "WHERE cs.store_id = e.store_id AND c.id = e.campaign_id "
        "AND c.id <> 'default' AND a.id = e.ad_id"
        ") "
        "GROUP BY store_total.store_count"
    )
    with pool().connection() as conn:
        row = conn.execute(query, (*store_params, month_start_utc, as_of_utc)).fetchone()

    store_count = int(row["store_count"] or 0)
    impressions = int(row["impressions"] or 0)
    clicks = int(row["clicks"] or 0)
    return {
        "scope": scope,
        "storeCount": store_count,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0,
        "period": {
            "from": month_start_jst.isoformat(),
            "through": as_of_jst.isoformat(),
            "timeZone": "Asia/Tokyo",
        },
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


def record_store_ad_event(event_type, public_code, campaign_id, ad_id, delivery_date):
    """Record a store delivery event only for the exact active assignment.

    The public code, campaign ID, and ad ID are untrusted request values. An
    INSERT ... SELECT verifies their complete relationship atomically through
    campaign_stores; ads.store_id is deliberately not consulted.
    """
    if event_type not in {"impression", "click"}:
        raise ValueError("invalid event type")
    if not isinstance(delivery_date, date) or isinstance(delivery_date, datetime):
        raise ValueError("delivery_date must be a date")
    occurred_at = datetime.now(timezone.utc)
    with pool().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO ad_events "
                "(event_type, store_id, store_name, ad_id, campaign_id, occurred_at) "
                "SELECT %s, s.id, s.name, a.id, c.id, %s "
                "FROM stores s "
                "JOIN campaign_stores cs ON cs.store_id = s.id "
                "JOIN campaigns c ON c.id = cs.campaign_id "
                "JOIN ads a ON a.id = c.ad_id "
                "WHERE s.public_code = %s AND c.id = %s AND a.id = %s "
                "AND c.id <> %s "
                "AND c.status = 'active' AND a.published IS TRUE "
                "AND (c.starts_on IS NULL OR c.starts_on <= %s) "
                "AND (c.ends_on IS NULL OR c.ends_on >= %s) "
                "RETURNING id",
                (event_type, occurred_at, public_code, campaign_id, ad_id, "default",
                 delivery_date, delivery_date),
            ).fetchone()
    return row is not None


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
