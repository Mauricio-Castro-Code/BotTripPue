"""
Puebla Travel Trips — FUENTE DE VERDAD.

Para actualizar viajes: edita data/viajes.json (agrega "tipo": "nacional" o "internacional")
Para actualizar pagos, requisitos y ubicación: edita este archivo.
"""
from __future__ import annotations
import json
from pathlib import Path


def _leer_viajes() -> list[dict]:
    ruta = Path(__file__).parent / "viajes.json"
    return json.loads(ruta.read_text(encoding="utf-8"))


def _resumen_por_tipo(tipo: str) -> str:
    viajes = [v for v in _leer_viajes() if v.get("tipo") == tipo]
    if not viajes:
        return "Por el momento no tenemos paquetes disponibles en esta categoría. Pronto traeremos novedades. 😊"
    lineas = [f"• *{v['destino']}* — {v['fecha_salida']}" for v in viajes]
    etiqueta = "🇲🇽 *Viajes Nacionales disponibles:*" if tipo == "nacional" else "🌎 *Viajes Internacionales disponibles:*"
    return (
        f"{etiqueta}\n\n"
        + "\n".join(lineas)
        + "\n\n¿Cuál te llama la atención? Dime el destino y te mando precio, fechas y todo lo que incluye. 😊\n"
        "¿Viste algo en nuestras redes que no está aquí? ¡También pregúntame! 📲"
    )


def _detalle_completo() -> str:
    viajes = _leer_viajes()
    bloques = []
    for v in viajes:
        incluye = "\n   ✅ ".join(v.get("incluye", []))
        bloques.append(
            f"✈️ *{v['destino']}* ({v.get('tipo', '')})\n"
            f"   📅 Salidas: {v['salidas']}\n"
            f"   🗓️ Próxima salida: {v['fecha_salida']}\n"
            f"   ⏱️ Duración: {v['no_dias']}\n"
            f"   💰 Precio: {v['precio']}\n"
            f"   🚌 Transporte: {v['transporte']}\n"
            f"   ✅ {incluye}\n"
            f"   💵 Reserva con: {v['reserva_con']}"
        )
    return "\n\n".join(bloques)


# Estas funciones se llaman en cada request para reflejar cambios en viajes.json sin reiniciar
def get_resumen_nacionales() -> str:
    return _resumen_por_tipo("nacional")

def get_resumen_internacionales() -> str:
    return _resumen_por_tipo("internacional")

def get_top10_internacionales() -> str:
    vistos: set[str] = set()
    viajes = []
    for v in _leer_viajes():
        if v.get("tipo") == "internacional" and v["destino"] not in vistos:
            vistos.add(v["destino"])
            viajes.append(v)
        if len(viajes) == 10:
            break
    if not viajes:
        return "Por el momento no tenemos paquetes internacionales disponibles. Pronto habrá novedades. 😊"
    lineas = [f"• *{v['destino']}*" for v in viajes]
    return (
        "🌎 *Nuestros destinos internacionales más populares:*\n\n"
        + "\n".join(lineas)
        + "\n\n¿Cuál te llama la atención? ✈️\n"
        "Dime el destino y te mando fecha, precio y todo lo que incluye. 😊\n"
        "¿Viste en nuestras redes un destino que no está aquí? ¡Pregúntame igual! 📲"
    )

def get_contexto_paquetes() -> str:
    return _detalle_completo()


# ─── Métodos de pago ──────────────────────────────────────────────────────────

METODOS_PAGO = """
💳 *Métodos de pago — Puebla Travel Trips*

• Transferencia bancaria / SPEI
• Tarjeta de crédito o débito (3 y 6 meses sin intereses)
• Efectivo en oficina

📌 *Política de apartado:*
Se requiere un anticipo del 30% para confirmar tu lugar.
El 70% restante se liquida 15 días antes de la salida.

¿Tienes alguna duda sobre los pagos? Con gusto te ayudamos 😊
"""

# ─── Requisitos ───────────────────────────────────────────────────────────────

REQUISITOS = """
📋 *Requisitos para viajar*

✅ Identificación oficial vigente (INE o pasaporte)
✅ Anticipo del 30% para apartar tu paquete
✅ Para viajes internacionales: pasaporte con mínimo 6 meses de vigencia
✅ Para vuelos: presentarse en el aeropuerto con 2 horas de anticipación
✅ Seguro de viaje (opcional, lo ofrecemos como complemento)

👶 *Menores de edad en viajes internacionales:*
• Pasaporte propio
• Carta permiso notariada si viaja sin uno o ambos padres

¿Tienes alguna duda? Escríbenos y con gusto te orientamos 😊
"""

# ─── Ubicación — reemplaza el link con el de tu oficina real ──────────────────

UBICACION = """
📍 *Visítanos en Puebla Travel Trips*

🗺️ Encuéntranos aquí:
https://maps.app.goo.gl/XXXXXXXXXXXXXX

⏰ *Horario de atención:*
Lunes a Viernes: 9:00 am – 7:00 pm
Sábado: 9:00 am – 2:00 pm

¡Te esperamos con gusto! ✈️
"""
