-- Migración 02: Módulo CRM — mensajes y estados comerciales
-- Ejecutar UNA sola vez en Supabase SQL Editor

-- ── Columnas nuevas en sesiones_ia ───────────────────────────────────────────
ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS estado_comercial VARCHAR(30) NOT NULL DEFAULT 'nuevo';
ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS score            INT         NOT NULL DEFAULT 0;
ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS requiere_humano  BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS asesor_nombre    VARCHAR(100);
ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS notas_internas   TEXT;

-- ── Tabla messages ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    sesion_id           UUID        NOT NULL REFERENCES sesiones_ia(id) ON DELETE CASCADE,
    telefono            VARCHAR(20) NOT NULL,
    canal               VARCHAR(20) NOT NULL DEFAULT 'whatsapp',
    direccion           VARCHAR(10) NOT NULL,   -- inbound | outbound
    sender_type         VARCHAR(10) NOT NULL,   -- cliente | bot | asesor | sistema
    sender_nombre       VARCHAR(100),
    body                TEXT        NOT NULL,
    whatsapp_message_id VARCHAR(100),
    status              VARCHAR(20) NOT NULL DEFAULT 'received',  -- received | sent | failed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_sesion   ON messages(sesion_id);
CREATE INDEX IF NOT EXISTS idx_messages_telefono ON messages(telefono);
