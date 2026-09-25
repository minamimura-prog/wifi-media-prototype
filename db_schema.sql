CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    store_type TEXT NOT NULL DEFAULT '',
    wifi TEXT NOT NULL DEFAULT '',
    monthly_users BIGINT NOT NULL DEFAULT 0 CHECK (monthly_users >= 0),
    legacy_clicks BIGINT NOT NULL DEFAULT 0 CHECK (legacy_clicks >= 0),
    status TEXT NOT NULL DEFAULT '稼働中',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS campaign_stores (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, store_id)
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

CREATE INDEX IF NOT EXISTS ad_events_occurred_at_idx ON ad_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ad_events_store_time_idx ON ad_events (store_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ad_events_ad_time_idx ON ad_events (ad_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS campaigns_status_dates_idx ON campaigns (status, starts_on, ends_on);
CREATE INDEX IF NOT EXISTS coupons_ad_idx ON coupons (ad_id);
CREATE INDEX IF NOT EXISTS coupon_events_occurred_at_idx ON coupon_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS coupon_events_coupon_time_idx ON coupon_events (coupon_id, occurred_at DESC);
