"""
CRM service — lógica de negocio para el panel de asesores.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .models import Message, SesionIA

logger = logging.getLogger(__name__)
_TZ_MX = ZoneInfo("America/Mexico_City")

_ESTADOS_SIN_BOT = {
    "requiere_humano", "en_atencion_humana", "reservando",
    "apartado", "pagado", "perdido", "finalizado",
}

ESTADOS_COMERCIALES = [
    "nuevo", "automatizado", "pregunto_info", "interesado",
    "requiere_humano", "en_atencion_humana", "reservando",
    "apartado", "pagado", "perdido", "finalizado",
]


# ─── Guardar mensajes ─────────────────────────────────────────────────────────

def guardar_mensaje_entrante(
    db: Session,
    sesion: SesionIA,
    body: str,
    whatsapp_message_id: str | None = None,
) -> None:
    msg = Message(
        sesion_id=sesion.id,
        telefono=sesion.telefono_cliente,
        canal=sesion.canal or "whatsapp",
        direccion="inbound",
        sender_type="cliente",
        body=body,
        whatsapp_message_id=whatsapp_message_id,
        status="received",
    )
    db.add(msg)
    db.commit()


def guardar_mensaje_saliente(
    db: Session,
    sesion: SesionIA,
    body: str,
    sender_type: str = "bot",
    sender_nombre: str | None = None,
) -> None:
    msg = Message(
        sesion_id=sesion.id,
        telefono=sesion.telefono_cliente,
        canal=sesion.canal or "whatsapp",
        direccion="outbound",
        sender_type=sender_type,
        sender_nombre=sender_nombre,
        body=body,
        status="sent",
    )
    db.add(msg)
    db.commit()


# ─── Control de bot ───────────────────────────────────────────────────────────

def debe_bot_responder(sesion: SesionIA) -> bool:
    if sesion.asesor_activo:
        return False
    estado = sesion.estado_comercial or "nuevo"
    if estado in _ESTADOS_SIN_BOT:
        return False
    return True


def pausar_bot(db: Session, sesion: SesionIA) -> None:
    sesion.asesor_activo = True
    sesion.asesor_desde = datetime.now(tz=_TZ_MX)
    db.commit()


def reactivar_bot(db: Session, sesion: SesionIA) -> None:
    sesion.asesor_activo = False
    sesion.asesor_desde = None
    db.commit()


# ─── Gestión de asesores ──────────────────────────────────────────────────────

def tomar_chat(db: Session, sesion: SesionIA, asesor_nombre: str) -> None:
    if (
        sesion.asesor_activo
        and sesion.asesor_nombre
        and sesion.asesor_nombre != asesor_nombre
    ):
        raise ValueError(f"Chat ya tomado por {sesion.asesor_nombre}")
    sesion.asesor_activo = True
    sesion.asesor_desde = datetime.now(tz=_TZ_MX)
    sesion.asesor_nombre = asesor_nombre
    sesion.estado_comercial = "en_atencion_humana"
    sesion.requiere_humano = False
    db.commit()


def liberar_chat(db: Session, sesion: SesionIA) -> None:
    sesion.asesor_activo = False
    sesion.asesor_desde = None
    sesion.asesor_nombre = None
    if (sesion.estado_comercial or "nuevo") == "en_atencion_humana":
        sesion.estado_comercial = "automatizado"
    db.commit()


# ─── Estados y metadatos ──────────────────────────────────────────────────────

def actualizar_estado_comercial(db: Session, sesion: SesionIA, estado: str) -> None:
    if estado not in ESTADOS_COMERCIALES:
        raise ValueError(f"Estado inválido: {estado}")
    sesion.estado_comercial = estado
    if estado == "requiere_humano":
        sesion.requiere_humano = True
    if estado in ("perdido", "finalizado"):
        sesion.sesion_cerrada = True
    db.commit()


def actualizar_score(db: Session, sesion: SesionIA, score: int) -> None:
    sesion.score = max(0, min(100, score))
    db.commit()


def agregar_nota(db: Session, sesion: SesionIA, nota: str) -> None:
    ahora = datetime.now(tz=_TZ_MX).strftime("%d/%m %H:%M")
    nueva_linea = f"[{ahora}] {nota.strip()}"
    sesion.notas_internas = (
        (sesion.notas_internas + "\n" + nueva_linea)
        if sesion.notas_internas
        else nueva_linea
    )
    db.commit()


# ─── Envío manual desde CRM ──────────────────────────────────────────────────

def enviar_mensaje_asesor(
    db: Session, sesion: SesionIA, asesor_nombre: str, body: str
) -> None:
    from .services import enviar_texto  # import local para evitar circular
    canal = sesion.canal or "whatsapp"
    enviar_texto(canal, sesion.telefono_cliente, body)
    guardar_mensaje_saliente(db, sesion, body, sender_type="asesor", sender_nombre=asesor_nombre)
    sesion.ultimo_mensaje = datetime.now(tz=_TZ_MX)
    db.commit()
