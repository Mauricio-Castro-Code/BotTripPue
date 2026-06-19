-- Migración 03: Historial acumulado de destinos de interés por lead
-- Ejecutar UNA sola vez en Supabase SQL Editor
--
-- Problema: leads.destino_interes se sobrescribe en cada mensaje, así que el
-- "Top destinos" del dashboard solo refleja el último destino mencionado por
-- cada lead, no todos los destinos por los que mostró interés. Esta tabla
-- registra cada destino distinto que un lead mencionó, una sola vez por par
-- (telefono, destino), para poder contar interés acumulado sin que se pierda
-- cuando el cliente pivota a otro destino.

CREATE TABLE IF NOT EXISTS lead_destinos (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    telefono   VARCHAR(20) NOT NULL,
    destino    VARCHAR(200) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (telefono, destino)
);

CREATE INDEX IF NOT EXISTS idx_lead_destinos_destino  ON lead_destinos(destino);
CREATE INDEX IF NOT EXISTS idx_lead_destinos_telefono ON lead_destinos(telefono);

-- ── Backfill: siembra la tabla con el destino actual de cada lead ───────────
-- (a partir de aquí, cada destino NUEVO que mencione un lead se agrega aparte;
-- esto solo recupera el último destino conocido para no arrancar en cero)
INSERT INTO lead_destinos (telefono, destino)
SELECT telefono, destino_interes
FROM leads
WHERE destino_interes IS NOT NULL
ON CONFLICT (telefono, destino) DO NOTHING;
