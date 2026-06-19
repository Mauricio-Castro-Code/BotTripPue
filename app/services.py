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
from urllib.parse import quote
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Lead, LeadDestino, SesionIA
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

NUMERO_ASESOR_NACIONAL       = "522228126321"   # Puebla Travel Trips
NUMERO_ASESOR_INTERNACIONAL  = "522225207938"   # LibertYa
NUMERO_COTIZACIONES          = "522212664376"   # Recibe el resumen de cotizaciones personalizadas
 
# ─── Menú ─────────────────────────────────────────────────────────────────────

_MENU_OPCIONES = (
    "¿En qué te podemos ayudar hoy?\n\n"
    "1️⃣ Viajes nacionales *(Puebla Travel Trips)*\n"
    "2️⃣ Viajes internacionales *(LibertYa)*\n"
    "3️⃣ Ya aparté un viaje / soy cliente\n"
    "4️⃣ Pagos, requisitos y más información\n"
    "5️⃣ Hablar con un asesor humano\n\n"
    "Responde con el número, la categoría o escribe directamente tu duda 👇🏼"
)

_MENSAJE_BIENVENIDA = (
    "¡Hola, viajero! 🌎✈️ Bienvenido a *LibertYa*, "
    "la nueva línea de viajes internacionales de la familia "
    "*Puebla Travel Trips*.\n\n"
    "Seguimos siendo el mismo equipo y la misma confianza que ya conoces, "
    "ahora listos para acompañarte a descubrir el mundo. 🧳🌍\n\n"
    "Soy tu asistente virtual. ¿Qué destino internacional te interesa conocer?\n\n"
    + _MENU_OPCIONES
)

_MENSAJE_DESPEDIDA = (
    "¡Hasta luego, viajero! 👋✈️\n\n"
    "Fue un placer atenderte. Cuando quieras planear tu próxima aventura, "
    "aquí estaremos. ¡Que tengas un excelente día! 🌟\n\n"
    "*LibertYa* — Tu agencia de confianza "
)

_RESPUESTAS_FIJAS: dict[str, str] = {
    "3": (
        "¡Claro, con gusto te ayudo! 😊\n\n"
        "¿Sobre qué viaje tienes una pregunta? Dime el destino o nombre del viaje que ya apartaste. ✈️"
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

# Estados que continúan con OpenAI después de la respuesta inicial
_ESTADOS_CON_IA = {
    "chat_nacional", "chat_internacional",
    "chat_cliente", "chat_cliente_nacional", "chat_cliente_internacional",
    "chat_grupo",
}

# ─── Sistema prompt OpenAI ────────────────────────────────────────────────────

_SISTEMA_BASE = """
Eres un asistente de WhatsApp de la familia de agencias *Puebla Travel Trips* (viajes nacionales) y *LibertYa* (viajes internacionales), con base en Puebla, México.
Responde siempre en español, de forma amable y concisa (máximo 3 párrafos cortos).

REGLA ABSOLUTA — INFORMACIÓN DE PAQUETES:
Cuando un cliente pregunte por un destino o paquete, busca en la sección PAQUETES DISPONIBLES la entrada que más se parezca a lo que pide.
- Si la encuentras: copia EXACTAMENTE los datos del catálogo (precio, fechas, duración, transporte, incluye, reserva). No parafrasees ni inventes datos distintos.
- Si NO la encuentras: responde amablemente que ese tour no está disponible aún, y ofrece que un asesor puede cotizárselo. Marca requiere_asesor=true.
NUNCA inventes precios, fechas, transportes, itinerarios, lugares a visitar, actividades ni ningún otro contenido de un paquete que no esté literalmente en PAQUETES DISPONIBLES. Si el catálogo no menciona un dato (por ejemplo duración exacta, lugares específicos, tamaño de grupo), NO lo inventes ni lo supongas: dilo abiertamente y ofrece que el asesor lo confirme.

NUNCA INVENTAR — REGLA DE ESCALAMIENTO:
Eres un asistente AUTOMATIZADO de preguntas frecuentes, no un asesor humano. Si no sabes algo, si la pregunta no se puede responder con los datos del catálogo (ej. itinerario detallado día por día, cambios de reserva, negociar precio), o si el cliente pide explícitamente hablar con una persona/asesor, NUNCA inventes una respuesta ni des un dato que no estés seguro. En su lugar, dile claramente que eres un asistente automático para dudas frecuentes y que un asesor humano puede ayudarlo mejor con eso, y marca requiere_asesor=true. El sistema le mandará automáticamente el contacto del asesor correcto (nacional o internacional) en el siguiente mensaje — tú NO necesitas dar el número ni el link, solo decir que lo vas a conectar.
Ejemplo concreto: si preguntan si un menor de edad puede ir, si hay restricción de edad, política de cancelación, reembolsos, descuentos no listados, o cualquier condición que el catálogo NO mencione explícitamente — NO asumas que aplica el mismo precio, que no hay restricción, o cualquier otra cosa. Di que eres un asistente automático y que el asesor lo confirmará, y marca requiere_asesor=true.

REGLA: Si en tu "respuesta" sugieres, mencionas o recomiendas que el cliente hable con un asesor/persona humana (con cualquier frase, ej. "un asesor te puede ayudar", "te recomendamos hablar con uno", "habla con nuestro equipo"), es OBLIGATORIO marcar requiere_asesor=true en esa misma llamada. Nunca sugieras contactar a un asesor sin activar el escalamiento — el cliente debe recibir el contacto real, no solo la sugerencia.

TU OBJETIVO ES VENDER: tu prioridad es ayudar a convertir al cliente en una reserva. Sé proactivo y resolutivo con la información del catálogo. Pero en el momento en que ya no puedas avanzar la venta tú mismo (falta un dato que no está en el catálogo, hay que negociar, el cliente duda o pone objeciones que no puedes resolver con el catálogo, o pide hablar con alguien), no te quedes dando vueltas: marca requiere_asesor=true para que un asesor humano tome el control y cierre la venta.

IMPORTANTE: Nunca incluyas links de contacto en tu respuesta. Los botones de acción los maneja el sistema automáticamente.

URGENCIA Y CONVERSIÓN:
Cuando el cliente muestre interés concreto en un paquete específico, añade al final de tu respuesta una frase de urgencia suave y natural. Ejemplos: "Este destino es muy solicitado y los lugares se llenan rápido, ¡te recomendamos apartar pronto! 🙌" o "Los cupos para esta salida son limitados, conviene asegurar tu lugar con anticipación. ✈️". No inventes números exactos de disponibilidad ni datos que no estén en el catálogo.

COTIZACIÓN GRUPAL — LEAD CALIENTE:
Cuando el cliente especifique la composición exacta de su grupo (número de adultos, niños o personas con edades), es la señal más clara de intención de compra. Responde con:
1. Desglose del costo total: "N adultos × $precio = $subtotal" + "N niños × $precio = $subtotal" + "**Total estimado: $XXX MXN**"
2. Si hay ambigüedad de edad (ej: 13-17 años pueden ser adulto o niño según la aerolínea), indícalo brevemente.
3. El monto de anticipo total si está en el catálogo.
4. Cierra con: "Usa el botón de abajo para hablar con el asesor y confirmar tu reserva. ✈️🙌"
NUNCA repitas la información del paquete que ya diste en el turno anterior — el cliente ya la tiene.
"""

_CONTEXTO_POR_ESTADO: dict[str, str] = {
    "menu": (
        "El cliente acaba de iniciar el chat y preguntó directamente por un destino específico "
        "sin pasar por el menú principal. Responde su pregunta con la información del catálogo. "
        "Si el destino es dentro de México, usa tipo_viaje='nacional'. "
        "Si es fuera de México, usa tipo_viaje='internacional'. "
        "Si el destino no está en el catálogo, díselo amablemente y ofrece alternativas similares. "
        "No incluyas links ni menciones al asesor."
    ),
    "chat_nacional": (
        "El cliente está interesado en viajes NACIONALES (Puebla Travel Trips). "
        "Ya se le preguntó qué tipo de viaje le interesa. Sigue estas reglas:\n"
        "1. Si menciona una categoría (playa, pueblo mágico, aventura, montaña, etc.) → muéstrale ÚNICAMENTE los destinos del catálogo que encajen, en lista breve con destino, fecha y precio.\n"
        "2. Si dice 'ver todos' o similar → muestra todos los destinos nacionales disponibles.\n"
        "3. Si menciona un destino específico que SÍ está en el catálogo → da todos los detalles: fechas, precio, duración, qué incluye, anticipo requerido.\n"
        "4. Si menciona un destino que NO está en el catálogo → dile amablemente que por el momento no tenemos ese paquete, pero que un asesor puede cotizárselo.\n"
        "Responde SOLO con información de destinos. No incluyas links ni menciones al asesor."
    ),
    "chat_internacional": (
        "El cliente está interesado en viajes INTERNACIONALES (LibertYa). "
        "Ya se le preguntó qué continente o región le interesa. Sigue estas reglas estrictamente:\n"
        "1. Si menciona un continente o región (Europa, América, Asia, Caribe, etc.) → filtra usando EXCLUSIVAMENTE el campo 'Continente' indicado en cada paquete del catálogo (PAQUETES DISPONIBLES). NUNCA infieras el continente por el nombre del país o ciudad — usa solo el valor literal de 'Continente' de cada entrada. Muéstrale ÚNICAMENTE los destinos cuyo campo Continente coincida, en formato de lista breve con destino y precio. Si un paquete combina dos continentes (ej. 'Europa y África'), inclúyelo en ambas búsquedas.\n"
        "2. Si dice 'ver todos', 'todos', 'los más populares', 'los más económicos' o similar → muéstrale una selección de máximo 10 destinos variados del catálogo ordenados de menor a mayor precio.\n"
        "3. Si menciona un país o destino específico que SÍ está en el catálogo → da todos los detalles: fechas, precio, duración, qué incluye, anticipo requerido. Recuérdales verificar vigencia del pasaporte si aplica.\n"
        "4. Si menciona un país que NO está en el catálogo → dile amablemente que por el momento no tenemos ese tour disponible, pero que un asesor puede cotizárselo con gusto.\n"
        "Responde SOLO con información de destinos. No incluyas links ni menciones al asesor."
    ),
    "chat_cliente": (
        "El cliente ya tiene una reserva y mencionó un viaje. Tu tarea en este turno es identificar "
        "si el viaje es NACIONAL (dentro de México) o INTERNACIONAL (fuera de México) y responder su duda. "
        "Usa el campo tipo_viaje para indicar 'nacional' o 'internacional' según corresponda. "
        "Si la duda es básica y el paquete está en el catálogo, respóndela. "
        "No incluyas links ni menciones al asesor."
    ),
    "chat_cliente_nacional": (
        "El cliente tiene una reserva en un viaje NACIONAL (Puebla Travel Trips). "
        "Responde sus dudas usando únicamente los datos del catálogo: fechas, precio, duración, qué incluye, anticipo. "
        "Si la pregunta es sobre procesos internos, cambios de reserva o pagos específicos que no están en el catálogo, "
        "indícale amablemente que un asesor lo ayudará mejor. No incluyas links ni menciones al asesor."
    ),
    "chat_cliente_internacional": (
        "El cliente tiene una reserva en un viaje INTERNACIONAL (LibertYa). "
        "Responde sus dudas usando únicamente los datos del catálogo: fechas, precio, duración, qué incluye, anticipo. "
        "Si la pregunta es sobre procesos internos, cambios de reserva o pagos específicos que no están en el catálogo, "
        "indícale amablemente que un asesor lo ayudará mejor. No incluyas links ni menciones al asesor."
    ),
    "chat_grupo": (
        "El cliente está pidiendo un VIAJE GRUPAL o una COTIZACIÓN PERSONALIZADA (empresa, escolar, boda, XV años, amigos, familia, etc.). "
        "Ya se le pidieron los 6 datos clave: 1) nombre del titular, 2) número de personas (adultos y niños con edad), "
        "3) destino, 4) número de días, 5) lugar de salida, 6) número de WhatsApp de contacto.\n"
        "- Si ya proporcionó los 6 datos: confirma los datos recibidos, dile que un asesor le preparará su cotización personalizada, "
        "marca requiere_asesor=true, y en resumen_cotizacion incluye un resumen formateado con los 6 datos exactos que dio el cliente.\n"
        "- Si aún falta algún dato: pregunta amablemente SOLO por el/los faltante(s) (no marques requiere_asesor ni llenes resumen_cotizacion todavía).\n"
        "Si el destino mencionado está en el catálogo, puedes compartir la info general como referencia, "
        "aclarando que para grupos/cotizaciones personalizadas se hace una cotización especial. "
        "No incluyas links ni menciones al asesor."
    ),
}

# ─── Clasificador de intención (OpenAI) ──────────────────────────────────────

_PROMPT_INTENT = """\
Eres el clasificador de intención del bot de WhatsApp de LibertYa, una agencia de viajes.

El cliente puede escribir de cualquier forma: con errores, informal, emojis, oraciones largas o muy cortas.

Clasifica el mensaje en UNA de estas intenciones:

saludo        → El cliente saluda o quiere reiniciar (hola, buenos días, inicio, start…)
despedida     → Se despide o ya terminó (adiós, bye, hasta luego, es todo, gracias fue todo…)
no_interesado → Indica que no le interesa o quiere cancelar (no me interesa, no gracias, ya no quiero, cancelar…)
menu_1        → Pregunta por viajes NACIONALES (dentro de México: playas, pueblos mágicos, Cancún, CDMX…)
menu_2        → Pregunta por viajes INTERNACIONALES (fuera de México: Europa, Caribe, Asia, cruceros…)
menu_3        → Ya apartó o compró un viaje, tiene una reserva, es cliente activo
menu_4        → Pregunta sobre precios, pagos, requisitos, documentos (SIN mencionar grupo)
menu_grupo    → Pregunta por un viaje GRUPAL: empresa, escolar, colegio, boda, XV años, amigos, equipo (cualquier viaje para un grupo de personas)
quiere_humano → Pide explícitamente hablar con una persona, asesor o humano real (no el bot)
continuar     → Cualquier otra cosa: pregunta específica de un destino, comentario, duda, algo que vio en redes

Estado actual de la conversación: {estado}

REGLA CRÍTICA: Si el estado es chat_nacional, chat_internacional, chat_cliente, chat_cliente_nacional, chat_cliente_internacional o chat_grupo,
y el mensaje es una pregunta, comentario o duda sobre un destino o viaje (aunque contenga
palabras cortas como "hi" o "hey" dentro de una oración en español), devuelve "continuar".
La única excepción es si el cliente pide explícitamente hablar con una persona/asesor humano — ahí devuelve "quiere_humano".

Devuelve ÚNICAMENTE una de estas palabras exactas: saludo, despedida, no_interesado, menu_1, menu_2, menu_3, menu_4, menu_grupo, quiere_humano, continuar\
"""

_VALIDAS_INTENT = frozenset({
    "saludo", "despedida", "no_interesado",
    "menu_1", "menu_2", "menu_3", "menu_4", "menu_grupo", "quiere_humano",
    "continuar",
})

_PALABRAS_GRUPO = frozenset({
    "escolar", "escolares", "colegio", "colegial", "preparatoria", "prepa",
    "universidad", "universitario", "facultad",
    "grupal", "grupales", "grupo", "grupos",
    "empresa", "empresarial", "corporativo", "corporativa",
    "boda", "bodas", "quinceañera", "quinceaños", "xv",
    "equipo", "equipos",
})

_FRASES_SUGIEREN_ASESOR = (
    "habla con un asesor", "habla con el asesor", "habla con nuestro asesor",
    "hablar con un asesor", "hablar con el asesor", "hablar con nuestro asesor",
    "contacta a un asesor", "contactar a un asesor", "contactar al asesor",
    "te recomendamos hablar con", "te recomiendo hablar con",
    "un asesor te puede ayudar", "un asesor te ayudará", "un asesor podrá",
    "un asesor lo ayudará", "un asesor la ayudará", "un asesor te ayudará",
    "un asesor puede cotizár", "un asesor puede darte", "un asesor te dará",
    "un asesor te preparará", "un asesor le preparará",
    "te conectamos con un asesor", "te conecto con un asesor",
)

_FRASES_HUMANO = (
    "hablar con una persona", "hablar con un humano", "hablar con alguien",
    "persona real", "ser humano", "atencion humana", "atención humana",
    "hablar con un asesor", "hablar con el asesor", "hablar con una agente",
    "quiero un asesor", "quiero hablar con", "necesito hablar con",
    "comunicarme con un asesor", "contactar a un asesor", "contactar al asesor",
    "agente humano", "no eres una persona", "no eres humano",
)


_MENU_DIRECTO = {"1": "menu_1", "2": "menu_2", "3": "menu_3", "4": "menu_4", "5": "quiere_humano"}


def clasificar_intencion(texto: str, estado: str) -> str:
    if estado == "menu" and texto.strip() in _MENU_DIRECTO:
        return _MENU_DIRECTO[texto.strip()]

    texto_lower = texto.lower()
    if any(frase in texto_lower for frase in _FRASES_HUMANO):
        return "quiere_humano"

    if estado != "chat_grupo":
        palabras_msg = set(texto_lower.split())
        if palabras_msg & _PALABRAS_GRUPO:
            return "menu_grupo"

    try:
        response = _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _PROMPT_INTENT.format(estado=estado)},
                {"role": "user", "content": texto},
            ],
            max_tokens=10,
            temperature=0,
        )
        resultado = response.choices[0].message.content.strip().lower()
        return resultado if resultado in _VALIDAS_INTENT else "continuar"
    except Exception as exc:
        logger.error("Error clasificando intención: %s", exc)
        return "continuar"


_ESTADOS_NACIONALES = {"chat_nacional", "chat_cliente_nacional"}
_ESTADOS_INTERNACIONALES = {"chat_internacional", "chat_cliente_internacional", "chat_cliente"}
_ESTADOS_AMBIGUOS_ASESOR = {"menu", "chat_cliente"}

_PREGUNTA_TIPO_ASESOR = (
    "¡Claro! 😊 Para conectarte con el asesor correcto, dime:\n\n"
    "1️⃣ Tu consulta es sobre viajes *nacionales* (Puebla Travel Trips)\n"
    "2️⃣ Tu consulta es sobre viajes *internacionales* (LibertYa)"
)


def _numero_asesor(estado: str) -> str:
    return NUMERO_ASESOR_NACIONAL if estado in _ESTADOS_NACIONALES else NUMERO_ASESOR_INTERNACIONAL


def get_ultima_duda(historial: list) -> str | None:
    for msg in reversed(historial):
        if msg.get("role") == "user":
            return msg.get("content")
    return None


def _mensaje_derivar(
    estado: str,
    viajes_interes: list[str] | None = None,
    duda_cliente: str | None = None,
) -> str:
    if estado in _ESTADOS_NACIONALES:
        nombre = "Puebla Travel Trips"
        numero = NUMERO_ASESOR_NACIONAL
    else:
        nombre = "LibertYa"
        numero = NUMERO_ASESOR_INTERNACIONAL

    if estado in ("chat_cliente_nacional", "chat_cliente_internacional") and duda_cliente:
        texto_precar = f"Hola, tengo una reserva y tengo la siguiente duda: {duda_cliente}"
        link = f"https://wa.me/{numero}?text={quote(texto_precar)}"
    elif viajes_interes:
        if len(viajes_interes) == 1:
            detalle = viajes_interes[0]
        else:
            detalle = ", ".join(viajes_interes[:-1]) + " y " + viajes_interes[-1]
        texto_precar = f"Hola, estoy interesado en reservar: {detalle}"
        link = f"https://wa.me/{numero}?text={quote(texto_precar)}"
    else:
        link = f"https://wa.me/{numero}"

    return (
        f"¡Con gusto! 😊 Escríbele directamente al asesor de *{nombre}*:\n\n"
        f"👉 {link}\n\n"
        "Te atenderá personalmente a la brevedad. ✈️"
    )


_ESTADO_POR_INTENCION: dict[str, str] = {
    "menu_1": "chat_nacional",
    "menu_2": "chat_internacional",
    "menu_3": "chat_cliente",
    "menu_4": "chat_cliente",
    "menu_grupo": "chat_grupo",
}

_OPCION_POR_INTENCION: dict[str, str] = {
    "menu_1": "1",
    "menu_2": "2",
    "menu_3": "3",
    "menu_4": "4",
    "menu_grupo": "grupo",
}


# ─── Estado de sesión ─────────────────────────────────────────────────────────

def get_estado(historial: list) -> str:
    for msg in historial:
        if msg.get("role") == "meta":
            return msg.get("estado", "menu")
    return "menu"


def get_viajes_interes(historial: list) -> list[str]:
    for msg in historial:
        if msg.get("role") == "meta":
            return list(msg.get("viajes_interes", []))
    return []


def set_estado(mensajes: list, estado: str, viajes_interes: list[str] | None = None) -> list:
    sin_meta = [m for m in mensajes if m.get("role") != "meta"]
    meta: dict = {"role": "meta", "estado": estado}
    if viajes_interes:
        meta["viajes_interes"] = viajes_interes
    return [meta] + sin_meta


def mensajes_openai(historial: list) -> list:
    return [m for m in historial if m.get("role") in ("user", "assistant")]


# ─── Sesión ───────────────────────────────────────────────────────────────────

def obtener_o_crear_sesion(db: Session, telefono: str, canal: str = "whatsapp") -> SesionIA:
    sesion = db.query(SesionIA).filter(SesionIA.telefono_cliente == telefono).first()
    if not sesion:
        sesion = SesionIA(telefono_cliente=telefono, historial=[], canal=canal)
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
        existe = (
            db.query(LeadDestino)
            .filter(LeadDestino.telefono == telefono, LeadDestino.destino == destino)
            .first()
        )
        if not existe:
            db.add(LeadDestino(telefono=telefono, destino=destino))
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
            "viaje_interes": {
                "type": "string",
                "description": (
                    "Resumen conciso del viaje ESPECÍFICO que le interesa al cliente, "
                    "solo si preguntó por un destino concreto del catálogo. "
                    "Formato: 'Destino (salida DD MMM, $precio)'. "
                    "Null si solo está explorando o preguntando de forma general."
                ),
            },
            "tipo_viaje": {
                "type": "string",
                "enum": ["nacional", "internacional"],
                "description": (
                    "Tipo del viaje que menciona el cliente. "
                    "'nacional' si el destino es dentro de México. "
                    "'internacional' si el destino es fuera de México. "
                    "Obligatorio cuando el estado sea 'chat_cliente' y el cliente mencione un viaje. "
                    "Null si no se puede determinar."
                ),
            },
            "requiere_asesor": {
                "type": "boolean",
                "description": (
                    "true si el cliente pidió explícitamente hablar con un humano/asesor, "
                    "o si el paquete/info que pide no está en el catálogo y no se puede responder con certeza, "
                    "o si en chat_grupo ya se recibieron los 6 datos (titular, personas, destino, días, lugar de salida, whatsapp). "
                    "false en cualquier otro caso."
                ),
            },
            "resumen_cotizacion": {
                "type": "string",
                "description": (
                    "SOLO en estado chat_grupo y cuando ya se recibieron los 6 datos: "
                    "resumen formateado con nombre del titular, personas (adultos y niños con edad), "
                    "destino, días, lugar de salida y número de WhatsApp de contacto, tal como los dio el cliente. "
                    "Null en cualquier otro caso."
                ),
            },
        },
        "required": ["respuesta"],
    },
}


_PREGUNTA_GRUPO = (
    "¡Perfecto! Nos especializamos en viajes grupales y personalizados 🎉\n\n"
    "Para prepararte tu cotización, cuéntanos:\n\n"
    "🙋 *Nombre del titular* de la cotización\n"
    "👥 *¿Cuántas personas viajarían?* (adultos y niños, con edad de los niños)\n"
    "📍 *¿A dónde quieren ir?* (destino)\n"
    "📅 *¿Cuántos días?*\n"
    "🛫 *¿Desde dónde salen?* (ciudad de origen)\n"
    "📱 *Número de WhatsApp* de contacto\n\n"
    "Con esos datos un asesor te preparará tu cotización personalizada. 😊"
)

_PREGUNTA_NACIONAL = (
    "¡Perfecto! 🇲🇽✈️ Tenemos salidas a distintos destinos dentro de México.\n\n"
    "¿Qué tipo de viaje te interesa?\n\n"
    "🏖️ *Playa* — Huatulco, Riviera Maya, Puerto Vallarta...\n"
    "🏘️ *Pueblo Mágico* — Destinos coloniales y con encanto\n"
    "🏔️ *Aventura / Naturaleza* — Ecoturismo, senderismo, cascadas...\n"
    "⭐ *Ver todos los disponibles*\n\n"
    "¡O dime directamente el destino que tienes en mente! 😊"
)

_PREGUNTA_CONTINENTE = (
    "¡Excelente elección! 🌎✈️ Tenemos destinos en varios continentes y regiones.\n\n"
    "¿Qué región te llama más la atención?\n\n"
    "🌍 *Europa* — España, Francia, Italia, Alemania, Marruecos...\n"
    "🌎 *América* — Colombia, Costa Rica, Panamá, Patagonia, Perú...\n"
    "🌏 *Asia / Oceanía* — Japón, Tailandia, India, Australia...\n"
    "🏝️ *Caribe* — Jamaica, Punta Cana, Cruceros...\n"
    "⭐ *Ver los más populares* — Te muestro una selección de los más solicitados\n\n"
    "¡O dime directamente el destino o país que tienes en mente! 😊"
)


def get_respuesta_opcion(opcion: str) -> str:
    if opcion == "1":
        return get_resumen_nacionales()
    if opcion == "2":
        return get_resumen_internacionales()
    if opcion == "grupo":
        return _PREGUNTA_GRUPO
    return _RESPUESTAS_FIJAS.get(opcion, "")


_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _fecha_es(dt: datetime) -> str:
    return f"{_DIAS_ES[dt.weekday()]} {dt.day} de {_MESES_ES[dt.month - 1]} de {dt.year}"


def _contexto_fecha() -> str:
    hoy = datetime.now(tz=_TZ_MX)
    dias_hasta_viernes = (4 - hoy.weekday()) % 7 or 7
    viernes = hoy + timedelta(days=dias_hasta_viernes)
    sabado = viernes + timedelta(days=1)
    domingo = viernes + timedelta(days=2)
    return (
        f"Fecha y hora actual (Ciudad de México): {_fecha_es(hoy)}, {hoy.strftime('%H:%M')}.\n"
        f"Próximo fin de semana: viernes {viernes.strftime('%d/%m')}, "
        f"sábado {sabado.strftime('%d/%m')}, domingo {domingo.strftime('%d/%m/%Y')}.\n"
        "Cuando el cliente diga 'este fin', 'este finde', 'este fin de semana' o similar, "
        "interpreta que se refiere a esas fechas exactas y busca en el catálogo si hay salidas esos días.\n"
        "NUNCA inventes ni asumas una fecha si no tienes certeza — usa siempre la fecha actual de arriba."
    )


def generar_respuesta_ia(mensajes: list, estado: str = "menu") -> tuple[str, str | None, str | None, str | None, str | None, bool, str | None]:
    """Llama a OpenAI. Devuelve (respuesta, nombre_detectado, destino_detectado, viaje_interes, tipo_viaje, requiere_asesor, resumen_cotizacion)."""
    contexto = _CONTEXTO_POR_ESTADO.get(estado, "")
    paquetes_actuales = get_contexto_paquetes()
    sistema = (
        _contexto_fecha()
        + "\n\n"
        + _SISTEMA_BASE
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
    respuesta = args["respuesta"]
    requiere_asesor = bool(args.get("requiere_asesor", False))
    respuesta_lower = respuesta.lower()
    if not requiere_asesor and any(frase in respuesta_lower for frase in _FRASES_SUGIEREN_ASESOR):
        requiere_asesor = True
    return (
        respuesta,
        args.get("nombre_detectado"),
        args.get("destino_detectado"),
        args.get("viaje_interes"),
        args.get("tipo_viaje"),
        requiere_asesor,
        args.get("resumen_cotizacion"),
    )


def notificar_cotizacion(resumen: str, telefono_cliente: str) -> None:
    """Envía el resumen de una cotización personalizada al número interno de cotizaciones."""
    mensaje = f"📋 Nueva cotización personalizada\n\n{resumen}\n\nTel. de contacto del cliente en WhatsApp: {telefono_cliente}"
    enviar_mensaje_texto(NUMERO_COTIZACIONES, mensaje)


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


# ─── Messenger ────────────────────────────────────────────────────────────────

def _post_messenger(payload: dict) -> None:
    url = f"{META_API_BASE}/me/messages"
    headers = {
        "Authorization": f"Bearer {settings.MESSENGER_PAGE_TOKEN}",
        "Content-Type": "application/json",
    }
    psid = payload.get("recipient", {}).get("id", "?")
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            logger.error("Messenger error a %s: %s — %s", psid, r.status_code, r.text)
    except Exception as exc:
        logger.error("Messenger error a %s: %s", psid, exc)


def enviar_texto_messenger(psid: str, mensaje: str) -> None:
    _post_messenger({"recipient": {"id": psid}, "message": {"text": mensaje}})


def enviar_botones_messenger(psid: str, estado: str = "") -> None:
    if estado in {"chat_cliente", "chat_cliente_nacional", "chat_cliente_internacional"}:
        titulo_accion = "Hablar con asesor"
        cuerpo = "¿Necesitas hablar directamente con un asesor? 😊"
    else:
        titulo_accion = "Apartar mi lugar"
        cuerpo = "¿Listo para asegurar tu lugar? Los cupos son limitados 🙌"

    _post_messenger({
        "recipient": {"id": psid},
        "message": {
            "text": cuerpo,
            "quick_replies": [
                {"content_type": "text", "title": titulo_accion,      "payload": "btn_reservar"},
                {"content_type": "text", "title": "Ya no me interesa","payload": "btn_terminar"},
            ],
        },
    })


# ─── Dispatcher multicanal ────────────────────────────────────────────────────

def enviar_texto(canal: str, sender: str, mensaje: str) -> None:
    if canal == "messenger":
        enviar_texto_messenger(sender, mensaje)
    else:
        enviar_mensaje_texto(sender, mensaje)


def enviar_botones(canal: str, sender: str, estado: str = "") -> None:
    if canal == "messenger":
        enviar_botones_messenger(sender, estado)
    else:
        enviar_botones_reserva(sender, estado)


# ─────────────────────────────────────────────────────────────────────────────

_ESTADOS_CLIENTE = {"chat_cliente", "chat_cliente_nacional", "chat_cliente_internacional"}


def enviar_botones_reserva(telefono: str, estado: str = "") -> None:
    if estado in _ESTADOS_CLIENTE:
        titulo_accion = "Hablar con asesor"
        cuerpo = "¿Necesitas hablar directamente con un asesor? 😊"
    else:
        titulo_accion = "Apartar mi lugar"
        cuerpo = "¿Listo para asegurar tu lugar? Los cupos son limitados 🙌"

    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_reservar", "title": titulo_accion}},
                    {"type": "reply", "reply": {"id": "btn_terminar", "title": "Ya no me interesa"}},
                ]
            },
        },
    })


# ─── Follow-up automático ─────────────────────────────────────────────────────

def _enviar_seguimiento_8h(telefono: str, canal: str = "whatsapp") -> None:
    """Primer aviso a las 8h de inactividad — invita a reservar o a resolver dudas."""
    texto_cuerpo = (
        "¡Hola! 👋 Vimos que estuviste explorando nuestros destinos de viaje.\n\n"
        "¿Pudiste encontrar lo que buscabas? Si tienes alguna duda o ya estás listo "
        "para apartar tu lugar, aquí estamos. ✈️"
    )
    if canal == "messenger":
        _post_messenger({
            "recipient": {"id": telefono},
            "message": {
                "text": texto_cuerpo,
                "quick_replies": [
                    {"content_type": "text", "title": "Quiero reservar",  "payload": "btn_reservar"},
                    {"content_type": "text", "title": "Tengo una duda",   "payload": "btn_seguir"},
                    {"content_type": "text", "title": "No me interesa",   "payload": "btn_no_interes"},
                ],
            },
        })
    else:
        _post_whatsapp({
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": texto_cuerpo},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "btn_reservar",   "title": "Quiero reservar"}},
                        {"type": "reply", "reply": {"id": "btn_seguir",     "title": "Tengo una duda"}},
                        {"type": "reply", "reply": {"id": "btn_no_interes", "title": "No me interesa"}},
                    ]
                },
            },
        })


def _enviar_seguimiento_derivacion(telefono: str) -> None:
    """24h después de ser derivado — pregunta si el asesor lo atendió."""
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "¡Hola! 👋 Queremos asegurarnos de que recibiste la atención que mereces.\n\n"
                    "¿Pudiste hablar con tu asesor? 😊"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_atendido",    "title": "Sí, me atendieron"}},
                    {"type": "reply", "reply": {"id": "btn_no_atendido", "title": "No, necesito ayuda"}},
                ]
            },
        },
    })


def _enviar_recordatorio_cierre(telefono: str, estado: str, canal: str = "whatsapp") -> None:
    """Segundo aviso a las 24h — advierte que el chat se cerrará en 2h."""
    nombre = "Puebla Travel Trips" if estado == "chat_nacional" else "LibertYa"
    texto_cuerpo = (
        f"¡Hola de nuevo! 😊 Desde {nombre} queremos recordarte "
        "que seguimos aquí para ayudarte a planear tu próximo viaje. ✈️\n\n"
        "Nuestros asesores están listos para reservar tu lugar y acompañarte "
        "en cada paso, ya sea un destino nacional o internacional. 🌎\n\n"
        "⚠️ Si no recibimos respuesta en las próximas 2 horas, "
        "cerraremos esta conversación automáticamente.\n"
        "¡Pero siempre estaremos aquí para cuando quieras viajar!"
    )
    if canal == "messenger":
        _post_messenger({
            "recipient": {"id": telefono},
            "message": {
                "text": texto_cuerpo,
                "quick_replies": [
                    {"content_type": "text", "title": "Quiero reservar",  "payload": "btn_reservar"},
                    {"content_type": "text", "title": "No me interesa",   "payload": "btn_no_interes"},
                ],
            },
        })
    else:
        _post_whatsapp({
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": texto_cuerpo},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "btn_reservar",   "title": "Quiero reservar"}},
                        {"type": "reply", "reply": {"id": "btn_no_interes", "title": "No me interesa"}},
                    ]
                },
            },
        })


def derivar_a_asesor(db: Session, sesion: SesionIA, canal: str, telefono: str, estado: str) -> None:
    """Envía el link del asesor humano y marca la sesión como derivada."""
    historial = list(sesion.historial or [])
    viajes_interes = get_viajes_interes(historial)
    estatus_derivado = "derivado_nacional" if estado in _ESTADOS_NACIONALES else "derivado_internacional"
    guardar_o_actualizar_lead(db, telefono, estatus=estatus_derivado)
    duda = get_ultima_duda(historial) if estado in ("chat_cliente_nacional", "chat_cliente_internacional") else None
    enviar_texto(canal, telefono, _mensaje_derivar(estado, viajes_interes, duda))
    sesion.derivado_at = datetime.now(tz=_TZ_MX)
    sesion.seguimiento_derivado = None
    sesion.asesor_activo = True
    sesion.asesor_desde = datetime.now(tz=_TZ_MX)
    db.commit()


def manejar_boton(db: Session, telefono: str, boton_id: str, canal: str = "whatsapp") -> None:
    sesion = obtener_o_crear_sesion(db, telefono, canal)
    estado = get_estado(list(sesion.historial or []))

    if boton_id in ("btn_no_interes", "btn_terminar"):
        guardar_historial(db, sesion, set_estado([], "cerrada"))
        sesion.sesion_cerrada = True
        db.commit()
        guardar_o_actualizar_lead(db, telefono, estatus="no_interesado")
        enviar_texto(canal, telefono, _MENSAJE_DESPEDIDA)

    elif boton_id in ("btn_asesor", "btn_reservar"):
        derivar_a_asesor(db, sesion, canal, telefono, estado)

    elif boton_id == "btn_atendido":
        guardar_historial(db, sesion, set_estado([], "cerrada"))
        sesion.sesion_cerrada = True
        db.commit()
        enviar_texto(
            canal, telefono,
            "¡Nos alegra mucho saberlo! 😊✈️\n\n"
            "Que disfrutes mucho tu viaje. ¡Hasta pronto y buen viaje! 🌍",
        )

    elif boton_id == "btn_no_atendido":
        historial = list(sesion.historial or [])
        viajes_interes = get_viajes_interes(historial)
        duda = get_ultima_duda(historial) if estado in ("chat_cliente_nacional", "chat_cliente_internacional") else None
        enviar_texto(canal, telefono, _mensaje_derivar(estado, viajes_interes, duda))

    elif boton_id == "btn_seguir":
        guardar_historial(db, sesion, set_estado(mensajes_openai(list(sesion.historial or [])), "menu"))
        enviar_texto(canal, telefono, _MENU_OPCIONES)


def registrar_echo_asesor(db: Session, telefono: str) -> None:
    """Marca que el asesor respondió manualmente (echo de Messenger). Renueva el timer de 24h."""
    sesion = db.query(SesionIA).filter(SesionIA.telefono_cliente == telefono).first()
    if sesion:
        sesion.asesor_activo = True
        sesion.asesor_desde = datetime.now(tz=_TZ_MX)
        db.commit()
        logger.info("Asesor tomó control de sesión: %s", telefono)


def _reactivar_bot(db: Session, sesion: SesionIA, canal: str) -> None:
    """Reactiva el bot y avisa al cliente."""
    sesion.asesor_activo = False
    sesion.asesor_desde = None
    db.commit()
    enviar_texto(
        canal, sesion.telefono_cliente,
        "¡Hola! 👋 Pasaron 24 horas desde que te conectamos con el asesor. "
        "Si aún tienes dudas o quieres retomar la conversación, aquí estoy. 😊",
    )


def procesar_seguimientos(db: Session) -> None:
    ahora = datetime.now(tz=_TZ_MX)

    # ── 1. Seguimiento 2h: primer aviso tras 2h de inactividad ───────────────
    sesiones_8h = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.ultimo_mensaje < ahora - timedelta(hours=2),
            or_(
                SesionIA.seguimiento_1h.is_(None),
                SesionIA.seguimiento_1h < SesionIA.ultimo_mensaje,
            ),
        )
        .all()
    )
    for sesion in sesiones_8h:
        try:
            _enviar_seguimiento_8h(sesion.telefono_cliente, sesion.canal)
            sesion.seguimiento_1h = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error seguimiento 8h a %s: %s", sesion.telefono_cliente, exc)

    # ── 2. Recordatorio 24h: segundo aviso con advertencia de cierre en 2h ───
    sesiones_24h = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.ultimo_mensaje < ahora - timedelta(hours=24),
            SesionIA.seguimiento_1h.isnot(None),
            SesionIA.seguimiento_3d.is_(None),
        )
        .all()
    )
    for sesion in sesiones_24h:
        try:
            estado_sesion = get_estado(list(sesion.historial or []))
            _enviar_recordatorio_cierre(sesion.telefono_cliente, estado_sesion, sesion.canal)
            sesion.seguimiento_3d = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error recordatorio 24h a %s: %s", sesion.telefono_cliente, exc)

    # ── 3. Auto-cierre: 2h después del recordatorio sin respuesta ────────────
    sesiones_cierre = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.seguimiento_3d.isnot(None),
            SesionIA.seguimiento_3d < ahora - timedelta(hours=2),
            SesionIA.ultimo_mensaje < SesionIA.seguimiento_3d,
        )
        .all()
    )
    for sesion in sesiones_cierre:
        try:
            sesion.sesion_cerrada = True
            db.commit()
            logger.info("Chat cerrado automáticamente: %s", sesion.telefono_cliente)
        except Exception as exc:
            logger.error("Error auto-cierre a %s: %s", sesion.telefono_cliente, exc)

    # ── 4. Seguimiento post-derivación: 24h después de ser derivado ──────────
    sesiones_derivadas = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.derivado_at.isnot(None),
            SesionIA.derivado_at < ahora - timedelta(hours=24),
            SesionIA.seguimiento_derivado.is_(None),
        )
        .all()
    )
    for sesion in sesiones_derivadas:
        try:
            _enviar_seguimiento_derivacion(sesion.telefono_cliente)
            sesion.seguimiento_derivado = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error seguimiento derivación a %s: %s", sesion.telefono_cliente, exc)

    # ── 5. Reactivar bot: 24h sin actividad del asesor (Opción A) ────────────
    sesiones_reactivar = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.asesor_activo == True,  # noqa: E712
            SesionIA.asesor_desde.isnot(None),
            SesionIA.asesor_desde < ahora - timedelta(hours=24),
        )
        .all()
    )
    for sesion in sesiones_reactivar:
        try:
            _reactivar_bot(db, sesion, getattr(sesion, "canal", "whatsapp"))
        except Exception as exc:
            logger.error("Error reactivando bot a %s: %s", sesion.telefono_cliente, exc)


# ─── Estadísticas para el dashboard ──────────────────────────────────────────

def get_estadisticas(db: Session) -> dict:
    ahora_dt = datetime.now(tz=_TZ_MX)

    # ── Resumen por estatus ───────────────────────────────────────────────────
    por_estatus_raw = (
        db.query(Lead.estatus, func.count(Lead.id))
        .group_by(Lead.estatus)
        .all()
    )
    por_estatus = {e: c for e, c in por_estatus_raw}
    total = sum(por_estatus.values())
    derivados = (
        por_estatus.get("derivado_nacional", 0)
        + por_estatus.get("derivado_internacional", 0)
    )
    tasa = round(derivados / total * 100, 1) if total else 0.0

    # ── Top destinos ──────────────────────────────────────────────────────────
    # Cuenta leads distintos por destino acumulado en lead_destinos, no el
    # último destino mencionado (que se sobrescribe en leads.destino_interes).
    top_destinos = (
        db.query(LeadDestino.destino, func.count(func.distinct(LeadDestino.telefono)).label("total"))
        .group_by(LeadDestino.destino)
        .order_by(func.count(func.distinct(LeadDestino.telefono)).desc())
        .limit(10)
        .all()
    )

    # ── Sesiones activas (conteo) ─────────────────────────────────────────────
    sesiones_activas = (
        db.query(func.count(SesionIA.id))
        .filter(SesionIA.sesion_cerrada == False)  # noqa: E712
        .scalar()
    ) or 0

    # ── Breakdown por canal ───────────────────────────────────────────────────
    canal_raw = (
        db.query(SesionIA.canal, func.count(SesionIA.id))
        .group_by(SesionIA.canal)
        .all()
    )
    por_canal = {c: n for c, n in canal_raw}

    # ── Actividad últimos 7 días ──────────────────────────────────────────────
    hace_7 = ahora_dt - timedelta(days=6)
    leads_dia_raw = (
        db.query(
            func.date(Lead.created_at).label("dia"),
            func.count(Lead.id).label("total"),
            func.sum(
                case((Lead.estatus.in_(["derivado_nacional", "derivado_internacional"]), 1), else_=0)
            ).label("derivados"),
        )
        .filter(Lead.created_at >= hace_7)
        .group_by(func.date(Lead.created_at))
        .order_by(func.date(Lead.created_at))
        .all()
    )
    dias_dict = {str(r.dia): {"total": r.total, "derivados": int(r.derivados or 0)} for r in leads_dia_raw}
    leads_por_dia = []
    for i in range(6, -1, -1):
        dia = (ahora_dt - timedelta(days=i)).date()
        d = dias_dict.get(str(dia), {"total": 0, "derivados": 0})
        leads_por_dia.append({"dia": dia.strftime("%d/%m"), "total": d["total"], "derivados": d["derivados"]})

    # ── Detalle de sesiones activas ───────────────────────────────────────────
    activas_raw = (
        db.query(SesionIA, Lead)
        .outerjoin(Lead, Lead.telefono == SesionIA.telefono_cliente)
        .filter(SesionIA.sesion_cerrada == False)  # noqa: E712
        .order_by(SesionIA.ultimo_mensaje.desc())
        .all()
    )
    detalle_activas = []
    for s, l in activas_raw:
        um = s.ultimo_mensaje
        if um and um.tzinfo is None:
            um = um.replace(tzinfo=_TZ_MX)
        mins = int((ahora_dt - um).total_seconds() / 60) if um else 0
        if mins < 60:
            ultima = f"hace {mins}m"
        elif mins < 1440:
            ultima = f"hace {mins // 60}h"
        else:
            ultima = f"hace {mins // 1440}d"
        detalle_activas.append({
            "tel": f"***{s.telefono_cliente[-4:]}",
            "nombre": (l.nombre or "—") if l else "—",
            "destino": (l.destino_interes or "—") if l else "—",
            "estatus": l.estatus if l else "nuevo",
            "estado_bot": get_estado(list(s.historial or [])),
            "canal": getattr(s, "canal", "whatsapp"),
            "ultima": ultima,
        })

    # ── Leads recientes ───────────────────────────────────────────────────────
    leads_recientes = [
        {
            "tel": f"***{l.telefono[-4:]}",
            "nombre": l.nombre or "—",
            "destino": l.destino_interes or "—",
            "estatus": l.estatus,
            "fecha": l.created_at.strftime("%d/%m %H:%M") if l.created_at else "—",
        }
        for l in db.query(Lead).order_by(Lead.created_at.desc()).limit(15).all()
    ]

    return {
        "total": total,
        "por_estatus": por_estatus,
        "derivados": derivados,
        "tasa_conversion": tasa,
        "top_destinos": list(top_destinos),
        "sesiones_activas": sesiones_activas,
        "por_canal": por_canal,
        "leads_por_dia": leads_por_dia,
        "detalle_activas": detalle_activas,
        "leads_recientes": leads_recientes,
        "fecha_actualizacion": ahora_dt.strftime("%d/%m/%Y %H:%M"),
    }


def broadcast_mensaje(db: Session, mensaje: str) -> dict:
    sesiones = (
        db.query(SesionIA)
        .filter(SesionIA.sesion_cerrada == False)  # noqa: E712
        .all()
    )
    enviados = 0
    fallidos = 0
    for sesion in sesiones:
        try:
            enviar_mensaje_texto(sesion.telefono_cliente, mensaje)
            estado = get_estado(list(sesion.historial or []))
            enviar_botones_reserva(sesion.telefono_cliente, estado)
            enviados += 1
        except Exception as exc:
            logger.error("Error en broadcast a %s: %s", sesion.telefono_cliente, exc)
            fallidos += 1
    return {"enviados": enviados, "fallidos": fallidos, "total": len(sesiones)}
