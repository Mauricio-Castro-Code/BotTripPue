import uuid

from sqlalchemy import Boolean, Column, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from .database import Base


class SesionIA(Base):
    __tablename__ = "sesiones_ia"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefono_cliente = Column(String(20), nullable=False, unique=True)
    historial        = Column(JSONB, nullable=False, default=list)
    ultimo_mensaje   = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    seguimiento_1h   = Column(DateTime(timezone=True), nullable=True)
    seguimiento_3d   = Column(DateTime(timezone=True), nullable=True)
    sesion_cerrada   = Column(Boolean, nullable=False, server_default=text("false"))
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
