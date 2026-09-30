CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    public_code TEXT,
    name TEXT NOT NULL,
    store_type TEXT NOT NULL DEFAULT '',
    wifi TEXT NOT NULL DEFAULT '',
    monthly_users BIGINT NOT NULL DEFAULT 0 CHECK (monthly_users >= 0),
    legacy_clicks BIGINT NOT NULL DEFAULT 0 CHECK (legacy_clicks >= 0),
    status TEXT NOT NULL DEFAULT '稼働中',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE stores ADD COLUMN IF NOT EXISTS public_code TEXT;
ALTER TABLE stores ALTER COLUMN public_code DROP NOT NULL;

DO $$
DECLARE
    store_row RECORD;
    base_code TEXT;
BEGIN
    FOR store_row IN
        SELECT id FROM stores
        WHERE public_code IS NULL
           OR btrim(public_code) = ''
           OR public_code !~ '^[a-z0-9]+(-[a-z0-9]+)*$'
        ORDER BY id
    LOOP
        -- Match database.public_code_for_store_id(): hash the lowercase
        -- hexadecimal UTF-8 bytes as ASCII text, then use one reserved prefix.
        -- This is bounded and independent of row order or other store IDs.
        base_code := 'store-' || md5(encode(convert_to(store_row.id, 'UTF8'), 'hex'));
        UPDATE stores SET public_code = base_code WHERE id = store_row.id;
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'stores_public_code_format_check'
          AND conrelid = 'stores'::regclass
    ) THEN
        ALTER TABLE stores
            ADD CONSTRAINT stores_public_code_format_check
            CHECK (public_code IS NULL OR public_code ~ '^[a-z0-9]+(-[a-z0-9]+)*$');
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS stores_public_code_uidx ON stores (public_code);

CREATE TABLE IF NOT EXISTS ads (
    id TEXT PRIMARY KEY,
    store_id TEXT REFERENCES stores(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    landing_url TEXT NOT NULL DEFAULT '',
    media_url TEXT NOT NULL DEFAULT '',
    starts_on DATE,
    ends_on DATE,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    starts_on DATE,
    ends_on DATE,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coupons (
    id TEXT PRIMARY KEY,
    ad_id TEXT REFERENCES ads(id) ON DELETE SET NULL,
    store_id TEXT REFERENCES stores(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    discount_type TEXT NOT NULL DEFAULT 'text',
    discount_value TEXT NOT NULL DEFAULT '',
    terms TEXT NOT NULL DEFAULT '',
    starts_on DATE,
    ends_on DATE,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE coupons ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS campaign_stores (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, store_id)
);

-- Persistent replay guard for store-campaign creation. Deliberately no foreign
-- keys: retain the key tombstone if a store/campaign/ad is later deleted so a
-- delayed retry cannot silently create a replacement campaign.
CREATE TABLE IF NOT EXISTS store_campaign_idempotency (
    idempotency_key TEXT PRIMARY KEY
        CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    store_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL
        CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    campaign_id TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ad_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('impression', 'click')),
    store_id TEXT REFERENCES stores(id) ON DELETE SET NULL,
    store_name TEXT NOT NULL DEFAULT '未設定',
    ad_id TEXT NOT NULL DEFAULT 'main',
    campaign_id TEXT REFERENCES campaigns(id) ON DELETE SET NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coupon_events (
    id BIGSERIAL PRIMARY KEY,
    coupon_id TEXT REFERENCES coupons(id) ON DELETE SET NULL,
    coupon_code TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    store_id TEXT REFERENCES stores(id) ON DELETE SET NULL,
    store_name TEXT NOT NULL DEFAULT '未設定',
    ad_id TEXT NOT NULL DEFAULT 'main',
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS store_settings (
    id TEXT PRIMARY KEY,
    store_id TEXT UNIQUE REFERENCES stores(id) ON DELETE CASCADE,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ad_events_occurred_at_idx ON ad_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ad_events_store_time_idx ON ad_events (store_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ad_events_ad_time_idx ON ad_events (ad_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS campaigns_status_dates_idx ON campaigns (status, starts_on, ends_on);
CREATE INDEX IF NOT EXISTS coupons_ad_idx ON coupons (ad_id);
CREATE INDEX IF NOT EXISTS coupon_events_occurred_at_idx ON coupon_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS coupon_events_coupon_time_idx ON coupon_events (coupon_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS admin_sessions_expires_at_idx ON admin_sessions (expires_at);

CREATE TABLE IF NOT EXISTS admin_login_limits (
    ip_address INET PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS admin_login_limits_updated_at_idx ON admin_login_limits (updated_at);
CREATE INDEX IF NOT EXISTS admin_login_limits_locked_until_idx ON admin_login_limits (locked_until)
    WHERE locked_until IS NOT NULL;
