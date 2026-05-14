-- ============================================================
-- PueblTrips — Esquema PostgreSQL
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- TABLA: leads
-- Registro de cada persona que escribió al WhatsApp
-- ============================================================
CREATE TABLE leads (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telefono        VARCHAR(20)  NOT NULL UNIQUE,
    nombre          VARCHAR(200),
    destino_interes VARCHAR(200),
    estatus         VARCHAR(30)  NOT NULL DEFAULT 'nuevo',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_leads_destino ON leads(destino_interes);

-- ============================================================
-- TABLA: sesiones_ia
-- Historial de conversación por cliente (contexto para OpenAI)
-- ============================================================
CREATE TABLE sesiones_ia (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telefono_cliente VARCHAR(20)  NOT NULL UNIQUE,
    historial        JSONB        NOT NULL DEFAULT '[]',
    ultimo_mensaje   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    seguimiento_1h   TIMESTAMPTZ,
    seguimiento_3d   TIMESTAMPTZ,
    sesion_cerrada   BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TRIGGER: updated_at automático
-- ============================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_leads_updated_at
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_sesiones_updated_at
    BEFORE UPDATE ON sesiones_ia
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- VISTA: destinos más populares
-- SELECT * FROM destinos_populares;
-- ============================================================
CREATE VIEW destinos_populares AS
SELECT
    destino_interes        AS destino,
    COUNT(*)               AS total_interesados,
    MAX(updated_at)        AS ultimo_contacto
FROM leads
WHERE destino_interes IS NOT NULL
GROUP BY destino_interes
ORDER BY total_interesados DESC;
