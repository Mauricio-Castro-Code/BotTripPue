"""
Capa de servicios — lógica del bot conversacional.

Responsabilidades:
  - Sesiones con máquina de estados (menu / chat_*)
  - Respuestas con OpenAI (chat completion + function calling)
  - Envío de mensajes a WhatsApp (Meta API)
  - Registro de leads
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Lead, SesionIA
from data.paquetes import (
    METODOS_PAGO,
    REQUISITOS,
    get_contexto_paquetes,
    get_resumen_nacionales,
    get_resumen_internacionales,
)

logger = logging.getLogger(__name__)
_TZ_MX = ZoneInfo("America/Mexico_City")
_openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)

META_API_BASE = "https://graph.facebook.com/v25.0"
_MAX_HISTORIAL = 20

NUMERO_ASESOR_NACIONAL       = "522212664376"   # Puebla Travel Trips
NUMERO_ASESOR_INTERNACIONAL  = "522222002327"   # LibertYa

# ─── Menú ─────────────────────────────────────────────────────────────────────

_MENU_OPCIONES = (
    "¿En qué te podemos ayudar hoy?\n\n"
    "1️⃣ Viajes nacionales *(Puebla Travel Trips)*\n"
    "2️⃣ Viajes internacionales *(LibertYa)*\n"
    "3️⃣ Ya aparté un viaje / soy cliente\n"
    "4️⃣ Pagos, requisitos y más información\n\n"
    "Responde con el número de tu opción 😊"
)

_MENSAJE_BIENVENIDA = (
    "¡Saludos viajero! 🌍✈️ Bienvenido a *LibertYa*, "
    "tu agencia de viajes de confianza.\n\n"
    "Soy tu asistente virtual y estoy aquí para ayudarte "
    "a planear tu próxima aventura. 😊\n\n"
    + _MENU_OPCIONES
)

_MENSAJE_DESPEDIDA = (
    "¡Hasta luego, viajero! 👋✈️\n\n"
    "Fue un placer atenderte. Cuando quieras planear tu próxima aventura, "
    "aquí estaremos. ¡Que tengas un excelente día! 🌟\n\n"
    "*LibertYa* — Tu agencia de confianza 😊"
)

_RESPUESTAS_FIJAS: dict[str, str] = {
    "3": (
        "¡Claro, con gusto te ayudo! 😊\n\n"
        "¿Sobre qué viaje tienes una duda y en qué te puedo orientar?"
    ),
    "4": (
        METODOS_PAGO
        + "\n\n"
        + REQUISITOS
        + "\n\n"
        "📌 *¿Viaje grupal?* (empresa, boda, XV años, amigos)\n"
        "Cuéntanos destino, número de personas y fechas tentativas y te preparamos una propuesta personalizada. 🎉"
    ),
}

_ESTADO_POR_OPCION: dict[str, str] = {
    "1": "chat_nacional",
    "2": "chat_internacional",
    "3": "chat_cliente",
    "4": "chat_cliente",
}

# Estados que continúan con OpenAI después de la respuesta inicial
_ESTADOS_CON_IA = {"chat_nacional", "chat_internacional", "chat_cliente", "chat_grupo"}

# ─── Sistema prompt OpenAI ────────────────────────────────────────────────────

_SISTEMA_BASE = """
Eres un asistente de WhatsApp de la familia de agencias *Puebla Travel Trips* (viajes nacionales) y *LibertYa* (viajes internacionales), con base en Puebla, México.
Responde siempre en español, de forma amable y concisa (máximo 3 párrafos cortos).
No inventes información que no esté en los paquetes disponibles.
Cuando el cliente muestre interés real en reservar o apartar un lugar:
- Si es viaje NACIONAL → dirígelo al asesor de Puebla Travel Trips: https://wa.me/522212664376
- Si es viaje INTERNACIONAL → dirígelo al asesor de LibertYa: https://wa.me/522222002327
"""

_CONTEXTO_POR_ESTADO: dict[str, str] = {
    "chat_nacional": (
        "El cliente está interesado en viajes NACIONALES (Puebla Travel Trips). "
        "Primero muéstrale los destinos nacionales del catálogo y pregúntale cuál le interesa. "
        "Si pregunta por un destino del catálogo, da todos los detalles (fechas, precio, qué incluye). "
        "Si quiere un viaje personalizado fuera del catálogo, pídele destino y fechas tentativas. "
        "Cuando muestre intención de reservar, dile que escriba al asesor: https://wa.me/522212664376"
    ),
    "chat_internacional": (
        "El cliente está interesado en viajes INTERNACIONALES (LibertYa). "
        "Primero muéstrale los destinos internacionales del catálogo y pregúntale cuál le interesa. "
        "Si pregunta por un destino del catálogo, da todos los detalles (fechas, precio, qué incluye). "
        "Si quiere un destino fuera del catálogo, pídele destino y fechas tentativas. Recuérdales verificar el pasaporte. "
        "Cuando muestre intención de reservar, dile que escriba al asesor: https://wa.me/522222002327"
    ),
    "chat_cliente": (
        "El cliente ya tiene una compra o tiene preguntas sobre pagos, requisitos o viajes grupales. "
        "Ayúdale con la información disponible. "
        "Para dudas de reserva nacional escríbele al asesor: https://wa.me/522212664376 "
        "Para dudas de reserva internacional: https://wa.me/522222002327"
    ),
}

# ─── Keywords ─────────────────────────────────────────────────────────────────

_KEYWORDS_SALUDO = {
    "hola", "buenas", "buenos días", "buenos dias", "buenas tardes",
    "buenas noches", "hi", "hey", "saludos", "buen día", "buen dia",
    "inicio", "menu", "menú", "start",
}

_KEYWORDS_DESPEDIDA = {
    "adiós", "adios", "hasta luego", "hasta pronto", "bye", "chao", "chau",
    "nos vemos", "es todo", "eso es todo", "es todo gracias", "ninguna duda",
    "no tengo dudas", "no hay dudas", "ya es todo", "gracias por todo",
    "muchas gracias", "mil gracias", "muy amable", "ya fue todo",
}


def es_saludo(texto: str) -> bool:
    return any(kw in texto.strip().lower() for kw in _KEYWORDS_SALUDO)


def es_despedida(texto: str) -> bool:
    return any(kw in texto.strip().lower() for kw in _KEYWORDS_DESPEDIDA)


_KEYWORDS_NO_INTERESA = {
    "no me interesa", "no interesa", "ya no me interesa", "no gracias", "no, gracias",
    "cancelar", "no quiero", "no necesito", "no por ahora", "tal vez después",
    "tal vez despues", "no por el momento", "dejame", "déjame", "no es lo que busco",
}


def es_no_interesado(texto: str) -> bool:
    return any(kw in texto.strip().lower() for kw in _KEYWORDS_NO_INTERESA)


def _numero_asesor(estado: str) -> str:
    return NUMERO_ASESOR_NACIONAL if estado == "chat_nacional" else NUMERO_ASESOR_INTERNACIONAL


def _mensaje_derivar(estado: str) -> str:
    if estado == "chat_nacional":
        nombre = "Puebla Travel Trips"
        numero = NUMERO_ASESOR_NACIONAL
    else:
        nombre = "LibertYa"
        numero = NUMERO_ASESOR_INTERNACIONAL
    return (
        f"¡Con gusto! 😊 Escríbele directamente al asesor de *{nombre}*:\n\n"
        f"👉 https://wa.me/{numero}\n\n"
        "Te atenderá personalmente a la brevedad. ✈️"
    )


_KEYWORDS_OPCION: dict[str, set[str]] = {
    "1": {"1", "nacional", "nacionales", "viajes nacionales", "viaje nacional", "mexico", "méxico", "puebla travel"},
    "2": {"2", "internacional", "internacionales", "viajes internacionales", "viaje internacional", "extranjero", "fuera del pais", "fuera del país", "libertya"},
    "3": {"3", "aparte", "aparté", "ya aparte", "ya aparté", "soy cliente", "cliente", "mi viaje", "mi reserva"},
    "4": {"4", "precio", "precios", "pago", "pagos", "costo", "costos", "cuanto cuesta", "cuánto cuesta", "formas de pago",
          "requisito", "requisitos", "documentos", "pasaporte", "visa",
          "grupo", "grupos", "grupal", "especial", "empresa", "boda", "xv", "quince", "info"},
}


def detectar_opcion_menu(texto: str) -> str | None:
    t = texto.strip().lower()
    for opcion, keywords in _KEYWORDS_OPCION.items():
        if any(kw in t for kw in keywords):
            return opcion
    return None


# ─── Estado de sesión ─────────────────────────────────────────────────────────

def get_estado(historial: list) -> str:
    for msg in historial:
        if msg.get("role") == "meta":
            return msg.get("estado", "menu")
    return "menu"


def set_estado(mensajes: list, estado: str) -> list:
    sin_meta = [m for m in mensajes if m.get("role") != "meta"]
    return [{"role": "meta", "estado": estado}] + sin_meta


def mensajes_openai(historial: list) -> list:
    return [m for m in historial if m.get("role") in ("user", "assistant")]


# ─── Sesión ───────────────────────────────────────────────────────────────────

def obtener_o_crear_sesion(db: Session, telefono: str) -> SesionIA:
    sesion = db.query(SesionIA).filter(SesionIA.telefono_cliente == telefono).first()
    if not sesion:
        sesion = SesionIA(telefono_cliente=telefono, historial=[])
        db.add(sesion)
        db.commit()
        db.refresh(sesion)
    return sesion


def guardar_historial(db: Session, sesion: SesionIA, historial: list) -> None:
    sesion.historial = historial
    sesion.ultimo_mensaje = datetime.now(tz=_TZ_MX)
    db.commit()


def preparar_historial(mensajes: list, mensaje_usuario: str) -> list:
    mensajes = list(mensajes) + [{"role": "user", "content": mensaje_usuario}]
    if len(mensajes) > _MAX_HISTORIAL:
        mensajes = mensajes[-_MAX_HISTORIAL:]
    return mensajes


# ─── Lead ─────────────────────────────────────────────────────────────────────

def guardar_o_actualizar_lead(
    db: Session,
    telefono: str,
    nombre: str | None = None,
    destino: str | None = None,
    estatus: str | None = None,
) -> None:
    lead = db.query(Lead).filter(Lead.telefono == telefono).first()
    if not lead:
        lead = Lead(telefono=telefono, estatus="nuevo")
        db.add(lead)
    if nombre and not lead.nombre:
        lead.nombre = nombre
    if destino:
        lead.destino_interes = destino
    if estatus:
        lead.estatus = estatus
    db.commit()


# ─── OpenAI ───────────────────────────────────────────────────────────────────

_FUNCION_RESPONDER = {
    "name": "responder_cliente",
    "description": "Genera la respuesta para el cliente y extrae información relevante.",
    "parameters": {
        "type": "object",
        "properties": {
            "respuesta": {
                "type": "string",
                "description": "Mensaje de respuesta para enviar al cliente por WhatsApp.",
            },
            "nombre_detectado": {
                "type": "string",
                "description": "Nombre del cliente si lo mencionó. Null si no.",
            },
            "destino_detectado": {
                "type": "string",
                "description": "Destino de viaje que le interesa si lo mencionó. Null si no.",
            },
        },
        "required": ["respuesta"],
    },
}


def get_respuesta_opcion(opcion: str) -> str:
    if opcion == "1":
        return get_resumen_nacionales()
    if opcion == "2":
        return get_resumen_internacionales()
    return _RESPUESTAS_FIJAS.get(opcion, "")


def generar_respuesta_ia(mensajes: list, estado: str = "menu") -> tuple[str, str | None, str | None]:
    """Llama a OpenAI. Devuelve (respuesta, nombre_detectado, destino_detectado)."""
    contexto = _CONTEXTO_POR_ESTADO.get(estado, "")
    paquetes_actuales = get_contexto_paquetes()
    sistema = (
        _SISTEMA_BASE
        + f"\nPAQUETES DISPONIBLES:\n{paquetes_actuales}"
        + (f"\n\nCONTEXTO ACTUAL: {contexto}" if contexto else "")
    )

    messages = [{"role": "system", "content": sistema}] + mensajes
    response = _openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=[{"type": "function", "function": _FUNCION_RESPONDER}],
        tool_choice={"type": "function", "function": {"name": "responder_cliente"}},
    )
    args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
    return (
        args["respuesta"],
        args.get("nombre_detectado"),
        args.get("destino_detectado"),
    )


# ─── WhatsApp ─────────────────────────────────────────────────────────────────

def _post_whatsapp(payload: dict) -> None:
    url = f"{META_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    telefono = payload.get("to", "?")
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            logger.error("Error enviando a %s: %s — %s", telefono, r.status_code, r.text)
        r.raise_for_status()
    except Exception as exc:
        logger.error("Error enviando a %s: %s", telefono, exc)


def enviar_mensaje_texto(telefono: str, mensaje: str) -> None:
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    })


# ─── Follow-up automático ─────────────────────────────────────────────────────

def _mensaje_seguimiento_3d(estado: str) -> str:
    numero = _numero_asesor(estado)
    nombre = "Puebla Travel Trips" if estado == "chat_nacional" else "LibertYa"
    return (
        f"Hola 👋 Tu conversación con *{nombre}* se cerrará en breve.\n\n"
        "Si sigues interesado en planear tu viaje, escríbele a nuestro asesor:\n"
        f"👉 https://wa.me/{numero}\n\n"
        "Si ya no necesitas información, puedes ignorar este mensaje. ¡Hasta pronto! ✈️"
    )


def enviar_catalogo_con_botones(telefono: str, catalogo: str) -> None:
    enviar_mensaje_texto(telefono, catalogo)
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "¿Te interesa algún destino? 😊"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_reservar", "title": "Quiero reservar"}},
                    {"type": "reply", "reply": {"id": "btn_terminar", "title": "Terminar chat"}},
                ]
            },
        },
    })


def enviar_botones_seguimiento_1d(telefono: str) -> None:
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "¡Hola! 👋 Vimos que estuviste explorando nuestros paquetes. "
                    "¿Te gustaría reservar tu lugar o tienes alguna duda sobre el proceso?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_reservar",   "title": "Quiero reservar"}},
                    {"type": "reply", "reply": {"id": "btn_seguir",     "title": "Tengo una duda"}},
                    {"type": "reply", "reply": {"id": "btn_no_interes", "title": "No me interesa"}},
                ]
            },
        },
    })


def enviar_seguimiento_3d(telefono: str) -> None:
    enviar_mensaje_texto(telefono, _MENSAJE_SEGUIMIENTO_3D)


def manejar_boton(db: Session, telefono: str, boton_id: str) -> None:
    sesion = obtener_o_crear_sesion(db, telefono)
    estado = get_estado(list(sesion.historial or []))

    if boton_id in ("btn_no_interes", "btn_terminar"):
        guardar_historial(db, sesion, set_estado([], "cerrada"))
        sesion.sesion_cerrada = True
        db.commit()
        guardar_o_actualizar_lead(db, telefono, estatus="no_interesado")
        enviar_mensaje_texto(telefono, _MENSAJE_DESPEDIDA)

    elif boton_id in ("btn_asesor", "btn_reservar"):
        estatus_derivado = "derivado_nacional" if estado == "chat_nacional" else "derivado_internacional"
        guardar_o_actualizar_lead(db, telefono, estatus=estatus_derivado)
        enviar_mensaje_texto(telefono, _mensaje_derivar(estado))

    elif boton_id == "btn_seguir":
        guardar_historial(db, sesion, set_estado(mensajes_openai(list(sesion.historial or [])), "menu"))
        enviar_mensaje_texto(telefono, _MENU_OPCIONES)


def procesar_seguimientos(db: Session) -> None:
    ahora = datetime.now(tz=_TZ_MX)

    sesiones_1h = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.ultimo_mensaje < ahora - timedelta(hours=8),
            or_(
                SesionIA.seguimiento_1h.is_(None),
                SesionIA.seguimiento_1h < SesionIA.ultimo_mensaje,
            ),
        )
        .all()
    )
    for sesion in sesiones_1h:
        try:
            enviar_botones_seguimiento_1d(sesion.telefono_cliente)
            sesion.seguimiento_1h = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error seguimiento 8h a %s: %s", sesion.telefono_cliente, exc)

    sesiones_3d = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.ultimo_mensaje < ahora - timedelta(days=3),
            SesionIA.seguimiento_1h.isnot(None),
            SesionIA.seguimiento_3d.is_(None),
        )
        .all()
    )
    for sesion in sesiones_3d:
        try:
            estado_sesion = get_estado(list(sesion.historial or []))
            enviar_mensaje_texto(sesion.telefono_cliente, _mensaje_seguimiento_3d(estado_sesion))
            sesion.seguimiento_3d = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error seguimiento 3d a %s: %s", sesion.telefono_cliente, exc)
