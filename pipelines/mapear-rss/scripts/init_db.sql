-- Cria o banco operacional do pipeline (URL Frontier + DLQ)
-- Executado automaticamente pelo container Postgres na inicialização

CREATE DATABASE mapear_rss;

\c mapear_rss;

-- URL Frontier: controle de URLs descobertas e status de extração
CREATE TABLE IF NOT EXISTS url_frontier (
    id BIGSERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    source_feed TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | completed | failed
    title TEXT,
    published_at TIMESTAMPTZ,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    recirculated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotent upgrade for pre-existing deployments.
ALTER TABLE url_frontier ADD COLUMN IF NOT EXISTS recirculated_at TIMESTAMPTZ;

CREATE INDEX idx_frontier_status ON url_frontier (status);
CREATE INDEX idx_frontier_source ON url_frontier (source_feed);
CREATE INDEX idx_frontier_discovered ON url_frontier (discovered_at DESC);

-- Dead Letter Queue: artigos que falharam na extração/processamento
CREATE TABLE IF NOT EXISTS failed_articles (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL,
    source_feed TEXT,
    error_type TEXT NOT NULL,
    error_message TEXT,
    stage TEXT NOT NULL,  -- extraction | transformation | enrichment
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    first_failure_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_failure_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (url, stage)
);

CREATE INDEX idx_failed_stage ON failed_articles (stage);
CREATE INDEX idx_failed_retry ON failed_articles (retry_count) WHERE resolved_at IS NULL;

-- Feed sources: cadastro de fontes RSS monitoradas
CREATE TABLE IF NOT EXISTS feed_sources (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    url TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    priority INTEGER NOT NULL DEFAULT 0,
    is_rn_focused BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_fetched_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
