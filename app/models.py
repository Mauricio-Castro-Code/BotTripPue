import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from .database import Base


class SesionIA(Base):
    __tablename__ = "sesiones_ia"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefono_cliente = Column(String(20), nullable=False, unique=True)
    canal            = Column(String(20), nullable=False, server_default=text("'whatsapp'"))
    historial        = Column(JSONB, nullable=False, default=list)
    ultimo_mensaje   = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    seguimiento_1h       = Column(DateTime(timezone=True), nullable=True)
    seguimiento_3d       = Column(DateTime(timezone=True), nullable=True)
    derivado_at          = Column(DateTime(timezone=True), nullable=True)
    seguimiento_derivado = Column(DateTime(timezone=True), nullable=True)
    sesion_cerrada       = Column(Boolean, nullable=False, server_default=text("false"))
    asesor_activo        = Column(Boolean, nullable=False, server_default=text("false"))
    asesor_desde         = Column(DateTime(timezone=True), nullable=True)
    # CRM fields
    estado_comercial = Column(String(30), nullable=False, server_default=text("'nuevo'"))
    score            = Column(Integer, nullable=False, server_default=text("0"))
    requiere_humano  = Column(Boolean, nullable=False, server_default=text("false"))
    asesor_nombre    = Column(String(100), nullable=True)
    notas_internas   = Column(Text, nullable=True)
    created_at       = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at       = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


class Lead(Base):
    __tablename__ = "leads"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefono        = Column(String(20), nullable=False, unique=True)
    nombre          = Column(String(200))
    destino_interes = Column(String(200))
    estatus         = Column(String(30), nullable=False, server_default=text("'nuevo'"))
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at      = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


class LeadDestino(Base):
    __tablename__ = "lead_destinos"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefono   = Column(String(20), nullable=False)
    destino    = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


class Message(Base):
    __tablename__ = "messages"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sesion_id           = Column(UUID(as_uuid=True), ForeignKey("sesiones_ia.id", ondelete="CASCADE"), nullable=False)
    telefono            = Column(String(20), nullable=False)
    canal               = Column(String(20), nullable=False, server_default=text("'whatsapp'"))
    direccion           = Column(String(10), nullable=False)   # inbound | outbound
    sender_type         = Column(String(10), nullable=False)   # cliente | bot | asesor | sistema
    sender_nombre       = Column(String(100), nullable=True)
    body                = Column(Text, nullable=False)
    whatsapp_message_id = Column(String(100), nullable=True)
    status              = Column(String(20), nullable=False, server_default=text("'received'"))
    created_at          = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
