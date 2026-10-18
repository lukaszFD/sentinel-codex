-- ============================================
-- Sentinel Codex — database knowledge_hub
-- Version: 1.0
-- ============================================
-- Runs as the `postgres` superuser via psql, connected to the `postgres`
-- maintenance database (playbook 01). Uses psql meta-commands (\gexec,
-- \connect), so it cannot run through a driver. Idempotent.
--
-- Lives in the Postgres instance of Cyber Sentinel (container postgres_db)
-- but is a separate database: Cyber Sentinel roles cannot connect to it,
-- and dropping or restoring one database never touches the other.
--
-- LAYOUT: one schema per module, each with its own roles:
--   news    News Radar (modules/news/db/10_news.sql)
-- ============================================

SELECT 'CREATE DATABASE knowledge_hub'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'knowledge_hub')\gexec

-- Only roles granted CONNECT by a module script may connect.
REVOKE ALL ON DATABASE knowledge_hub FROM PUBLIC;

\connect knowledge_hub

-- Nothing lives in `public`; modules use their own schemas.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- ============================================
-- VERIFICATION (run manually)
-- ============================================
-- \l knowledge_hub
-- SELECT has_database_privilege('<cyber sentinel role>', 'knowledge_hub', 'CONNECT');  -- f
-- ============================================
