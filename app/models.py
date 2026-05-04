"""
Modelos SQLAlchemy — espejo exacto del esquema en db/schema.sql.
Cada clase mapea 1:1 a su tabla; no se agregan columnas fuera del schema.
"""
import uuid

from sqlalchemy import (
    Boolean, Column, Computed, Date, DateTime, Enum,
    ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .database import Base


class Negocio(Base):
    __tablename__ = "negocios"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre        = Column(String(200), nullable=False)
    api_key_meta  = Column(Text, nullable=False, unique=True)
    verify_token  = Column(Text, nullable=False)
    logo_url      = Column(Text)
    terminos      = Column(Text)
    activo        = Column(Boolean, nullable=False, default=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at    = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    inventario   = relationship("Inventario", back_populates="negocio")
    cotizaciones = relationship("Cotizacion", back_populates="negocio")
    sesiones     = relationship("SesionIA", back_populates="negocio")


class Inventario(Base):
    __tablename__ = "inventario"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    negocio_id      = Column(UUID(as_uuid=True), ForeignKey("negocios.id", ondelete="CASCADE"), nullable=False)
    nombre_producto = Column(String(200), nullable=False)
    descripcion     = Column(Text)
    precio_renta    = Column(Numeric(10, 2), nullable=False)
    stock_total     = Column(Integer, nullable=False, default=0)
    activo          = Column(Boolean, nullable=False, default=True)
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at      = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    negocio  = relationship("Negocio", back_populates="inventario")
    detalles = relationship("DetalleCotizacion", back_populates="producto")


class Cliente(Base):
    __tablename__ = "clientes"

    id                      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre                  = Column(String(200))
    telefono_whatsapp       = Column(String(20), nullable=False, unique=True)
    direccion_predeterminada = Column(Text)
    created_at              = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at              = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    cotizaciones = relationship("Cotizacion", back_populates="cliente")


class Cotizacion(Base):
    __tablename__ = "cotizaciones"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    negocio_id  = Column(UUID(as_uuid=True), ForeignKey("negocios.id"), nullable=False)
    cliente_id  = Column(UUID(as_uuid=True), ForeignKey("clientes.id"), nullable=False)
    total       = Column(Numeric(12, 2), nullable=False, default=0)
    estatus     = Column(
        Enum("borrador", "enviado", "aceptado", "cancelado", name="estatus_cotizacion"),
        nullable=False,
        default="borrador",
    )
    pdf_url     = Column(Text)
    notas       = Column(Text)
    fecha_evento = Column(Date)
    folio_cotizacion   = Column(String(20), unique=True)         # COTI00001-26 (al crear)
    folio_pedido       = Column(String(20), unique=True)         # 00001-26 (al confirmar)
    confirmada         = Column(Boolean, nullable=False, default=False)
    fecha_confirmacion = Column(DateTime(timezone=True))
    created_at  = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at  = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    negocio  = relationship("Negocio", back_populates="cotizaciones")
    cliente  = relationship("Cliente", back_populates="cotizaciones")
    detalles = relationship("DetalleCotizacion", back_populates="cotizacion", cascade="all, delete-orphan")


class DetalleCotizacion(Base):
    __tablename__ = "detalle_cotizacion"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cotizacion_id   = Column(UUID(as_uuid=True), ForeignKey("cotizaciones.id", ondelete="CASCADE"), nullable=False)
    producto_id     = Column(UUID(as_uuid=True), ForeignKey("inventario.id"), nullable=False)
    cantidad        = Column(Integer, nullable=False)
    precio_unitario = Column(Numeric(10, 2), nullable=False)
    # Columna GENERATED ALWAYS en PostgreSQL — solo lectura desde Python
    subtotal        = Column(Numeric(12, 2), Computed("cantidad * precio_unitario", persisted=True))
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    cotizacion = relationship("Cotizacion", back_populates="detalles")
    producto   = relationship("Inventario", back_populates="detalles")


class SesionIA(Base):
    """
    Estado de la conversación WhatsApp ↔ IA.
    contexto_actual acumula los campos extraídos durante la conversación.
    Solo puede existir UNA sesión activa por (telefono, negocio).
    """
    __tablename__ = "sesiones_ia"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefono_cliente = Column(String(20), nullable=False)
    negocio_id       = Column(UUID(as_uuid=True), ForeignKey("negocios.id"), nullable=False)
    contexto_actual  = Column(JSONB, nullable=False, default=dict)
    cotizacion_id    = Column(UUID(as_uuid=True), ForeignKey("cotizaciones.id"), nullable=True)
    activa           = Column(Boolean, nullable=False, default=True)
    ultimo_mensaje   = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    created_at       = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at       = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    negocio    = relationship("Negocio", back_populates="sesiones")
    cotizacion = relationship("Cotizacion", foreign_keys=[cotizacion_id])

    __table_args__ = (
        UniqueConstraint("telefono_cliente", "negocio_id", "activa", name="uq_sesion_activa"),
    )
