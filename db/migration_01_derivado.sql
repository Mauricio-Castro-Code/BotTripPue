-- Migración 01: columnas para seguimiento post-derivación
-- Ejecutar UNA sola vez en la base de datos de producción (Railway)

ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS derivado_at          TIMESTAMPTZ;
ALTER TABLE sesiones_ia ADD COLUMN IF NOT EXISTS seguimiento_derivado TIMESTAMPTZ;
