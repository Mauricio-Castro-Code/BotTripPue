-- ============================================================
-- BotCotizar - Esquema Multi-tenant (PostgreSQL)
-- ============================================================

-- Extensión para UUIDs (más seguro que IDs secuenciales en SaaS)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- TABLA: negocios
-- Un "tenant" = un negocio cliente del SaaS
-- ============================================================
CREATE TABLE negocios (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre        VARCHAR(200)  NOT NULL,
    api_key_meta  TEXT          NOT NULL UNIQUE,  -- Token de acceso de Meta/WhatsApp
    verify_token  TEXT          NOT NULL,         -- Token de verificación del webhook
    logo_url      TEXT,
    terminos      TEXT,
    activo        BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TABLA: inventario
-- Catálogo de productos por negocio
-- ============================================================
CREATE TABLE inventario (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    negocio_id      UUID         NOT NULL REFERENCES negocios(id) ON DELETE CASCADE,
    nombre_producto VARCHAR(200) NOT NULL,
    descripcion     TEXT,
    precio_renta    NUMERIC(10,2) NOT NULL CHECK (precio_renta >= 0),
    stock_total     INTEGER       NOT NULL DEFAULT 0 CHECK (stock_total >= 0),
    activo          BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_inventario_negocio    ON inventario(negocio_id);
CREATE INDEX idx_inventario_nombre     ON inventario(negocio_id, nombre_producto);

-- ============================================================
-- TABLA: clientes
-- Contactos de WhatsApp (compartidos entre negocios)
-- ============================================================
CREATE TABLE clientes (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre                VARCHAR(200),
    telefono_whatsapp     VARCHAR(20)  NOT NULL UNIQUE,  -- Formato E.164: +521234567890
    direccion_predeterminada TEXT,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_clientes_telefono ON clientes(telefono_whatsapp);

-- ============================================================
-- TABLA: cotizaciones
-- Cabecera del documento de cotización
-- ============================================================
CREATE TYPE estatus_cotizacion AS ENUM ('borrador', 'enviado', 'aceptado', 'cancelado');

CREATE TABLE cotizaciones (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    negocio_id  UUID                 NOT NULL REFERENCES negocios(id),
    cliente_id  UUID                 NOT NULL REFERENCES clientes(id),
    total       NUMERIC(12,2)        NOT NULL DEFAULT 0,
    estatus     estatus_cotizacion   NOT NULL DEFAULT 'borrador',
    pdf_url     TEXT,
    notas       TEXT,
    fecha_evento DATE,                          -- Fecha del evento a rentar
    folio_cotizacion   VARCHAR(20) UNIQUE,      -- COTI00001-26 (al crear cotizacion)
    folio_pedido       VARCHAR(20) UNIQUE,      -- 00001-26 (solo cuando el cliente confirma)
    confirmada         BOOLEAN     NOT NULL DEFAULT FALSE,
    fecha_confirmacion TIMESTAMPTZ,
    created_at  TIMESTAMPTZ          NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ          NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cotizaciones_negocio      ON cotizaciones(negocio_id);
CREATE INDEX idx_cotizaciones_cliente      ON cotizaciones(cliente_id);
CREATE INDEX idx_cotizaciones_estatus      ON cotizaciones(estatus);
CREATE INDEX idx_cotizaciones_confirmada   ON cotizaciones(confirmada);
CREATE INDEX idx_cotizaciones_folio_pedido ON cotizaciones(folio_pedido) WHERE folio_pedido IS NOT NULL;

-- ============================================================
-- TABLA: detalle_cotizacion
-- Líneas de artículos dentro de una cotización
-- ============================================================
CREATE TABLE detalle_cotizacion (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cotizacion_id   UUID          NOT NULL REFERENCES cotizaciones(id) ON DELETE CASCADE,
    producto_id     UUID          NOT NULL REFERENCES inventario(id),
    cantidad        INTEGER       NOT NULL CHECK (cantidad > 0),
    precio_unitario NUMERIC(10,2) NOT NULL,  -- Captura el precio al momento de cotizar
    subtotal        NUMERIC(12,2) GENERATED ALWAYS AS (cantidad * precio_unitario) STORED,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_detalle_cotizacion ON detalle_cotizacion(cotizacion_id);

-- ============================================================
-- TABLA: sesiones_ia
-- Estado de la conversación de WhatsApp con la IA
-- ============================================================
CREATE TABLE sesiones_ia (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telefono_cliente  VARCHAR(20)  NOT NULL,
    negocio_id        UUID         NOT NULL REFERENCES negocios(id),
    contexto_actual   JSONB        NOT NULL DEFAULT '{}',  -- Datos extraídos hasta ahora
    cotizacion_id     UUID         REFERENCES cotizaciones(id),  -- Se llena al cerrar sesión
    activa            BOOLEAN      NOT NULL DEFAULT TRUE,
    ultimo_mensaje    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Solo puede existir UNA sesión activa por teléfono+negocio
    UNIQUE (telefono_cliente, negocio_id, activa)
);

CREATE INDEX idx_sesiones_telefono  ON sesiones_ia(telefono_cliente, negocio_id);
CREATE INDEX idx_sesiones_activa    ON sesiones_ia(activa) WHERE activa = TRUE;
-- Índice GIN para búsquedas dentro del JSONB
CREATE INDEX idx_sesiones_contexto  ON sesiones_ia USING GIN (contexto_actual);

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

CREATE TRIGGER trg_negocios_updated_at
    BEFORE UPDATE ON negocios
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_inventario_updated_at
    BEFORE UPDATE ON inventario
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_clientes_updated_at
    BEFORE UPDATE ON clientes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_cotizaciones_updated_at
    BEFORE UPDATE ON cotizaciones
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_sesiones_updated_at
    BEFORE UPDATE ON sesiones_ia
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- FUNCIÓN: aceptar_cotizacion
-- Cambia el estatus y descuenta stock. Es ATÓMICA (todo o nada).
-- ============================================================
CREATE OR REPLACE FUNCTION aceptar_cotizacion(p_cotizacion_id UUID)
RETURNS VOID AS $$
DECLARE
    v_item RECORD;
BEGIN
    -- Verificar que la cotización existe y está en estado válido
    IF NOT EXISTS (
        SELECT 1 FROM cotizaciones
        WHERE id = p_cotizacion_id AND estatus = 'enviado'
    ) THEN
        RAISE EXCEPTION 'La cotización % no existe o no está en estatus "enviado"', p_cotizacion_id;
    END IF;

    -- Verificar stock suficiente para cada producto ANTES de descontar
    FOR v_item IN
        SELECT dc.producto_id, dc.cantidad, i.nombre_producto, i.stock_total
        FROM detalle_cotizacion dc
        JOIN inventario i ON i.id = dc.producto_id
        WHERE dc.cotizacion_id = p_cotizacion_id
    LOOP
        IF v_item.stock_total < v_item.cantidad THEN
            RAISE EXCEPTION 'Stock insuficiente para "%": disponible=%, requerido=%',
                v_item.nombre_producto, v_item.stock_total, v_item.cantidad;
        END IF;
    END LOOP;

    -- Descontar stock (dentro de la misma transacción)
    UPDATE inventario i
    SET stock_total = i.stock_total - dc.cantidad
    FROM detalle_cotizacion dc
    WHERE dc.cotizacion_id = p_cotizacion_id
      AND i.id = dc.producto_id;

    -- Cambiar estatus
    UPDATE cotizaciones
    SET estatus = 'aceptado'
    WHERE id = p_cotizacion_id;

END;
$$ LANGUAGE plpgsql;
