"""
Puebla Travel Trips — FUENTE DE VERDAD.

Para actualizar viajes: edita data/viajes.json (agrega "tipo": "nacional" o "internacional")
Para actualizar pagos, requisitos y ubicación: edita este archivo.

Campos opcionales en viajes.json:
  fechas          → lista de fechas de salida (reemplaza fecha_salida)
  estado          → región geográfica ("Morelos", "Quintana Roo"...)
  horario_salida  → hora de salida para excursiones de 1 día ("06:30 am")
  horario_regreso → hora de regreso para excursiones de 1 día ("10:30 pm")
  notas           → texto libre para notas especiales del tour
"""
from __future__ import annotations
import json
from pathlib import Path


def _leer_viajes() -> list[dict]:
    ruta = Path(__file__).parent / "viajes.json"
    return json.loads(ruta.read_text(encoding="utf-8"))


def _primera_fecha(v: dict) -> str:
    fechas = v.get("fechas", [])
    return fechas[0] if fechas else "Por confirmar"


def _formatear_viaje(v: dict) -> str:
    lines = [f"*{v['destino']}*", f"💰 {v['precio']}", ""]

    # Fechas
    fechas = [f for f in v.get("fechas", []) if f]
    if fechas:
        lines.append("📆 *Fechas de salida:*")
        for f in fechas:
            lines.append(f"• {f}")
        lines.append("")

    # Ubicación (solo si hay estado o punto de salida específico)
    estado = v.get("estado", "")
    salidas = v.get("salidas", "")
    punto_especifico = salidas not in ("", "No especificado", "Puebla")
    if estado or punto_especifico:
        loc = "📍 *Dónde:*"
        if estado:
            loc += f" {estado}"
        lines.append(loc)
        lines.append("Salida desde Puebla")
        if punto_especifico:
            lines.append(f"({salidas})")
        lines.append("")

    # Horarios (excursiones de 1 día)
    horario_salida = v.get("horario_salida", "")
    horario_regreso = v.get("horario_regreso", "")
    if horario_salida or horario_regreso:
        if horario_salida:
            lines.append(f"⏰ Salida: {horario_salida}")
        if horario_regreso:
            lines.append(f"⏰ Regreso: {horario_regreso}")
        lines.append("")
    else:
        # Duración y transporte en una sola línea cuando ambos están disponibles
        no_dias = v.get("no_dias", "")
        transporte = v.get("transporte", "")
        tiene_duracion = no_dias and no_dias != "No especificado"
        tiene_transporte = transporte and transporte != "No especificado"
        if tiene_duracion and tiene_transporte:
            lines.append(f"⏱️ {no_dias}  ·  ✈️ {transporte}")
        elif tiene_duracion:
            lines.append(f"⏱️ *Duración:* {no_dias}")
        elif tiene_transporte:
            lines.append(f"✈️ *Transporte:* {transporte}")
        if tiene_duracion or tiene_transporte:
            lines.append("")

    # Ciudades que visita (internacionales)
    lugares = v.get("lugares", [])
    if lugares:
        lines.append("🗺️ *Ciudades que visitas:*")
        lines.append("   " + " · ".join(lugares))
        lines.append("")

    # Qué incluye
    incluye = v.get("incluye", [])
    if incluye:
        lines.append("✅ *Incluye:*")
        for item in incluye:
            lines.append(item)
        lines.append("")

    # Requisitos especiales
    requisitos = v.get("requisitos", [])
    if requisitos:
        lines.append("⚠️ *Requisitos:*")
        for req in requisitos:
            lines.append(f"   {req}")
        lines.append("")

    # Nota + reserva agrupadas al final
    notas = v.get("notas", "")
    reserva = v.get("reserva_con", "")
    tiene_reserva = reserva and reserva != "No especificado"
    if notas or tiene_reserva:
        if notas:
            lines.append(f"📌 {notas}")
        if tiene_reserva:
            lines.append(f"💵 {reserva}")

    return "\n".join(lines).strip()


def _resumen_por_tipo(tipo: str) -> str:
    viajes = [v for v in _leer_viajes() if v.get("tipo") == tipo]
    if not viajes:
        return "Por el momento no tenemos paquetes disponibles en esta categoría. Pronto traeremos novedades. 😊"
    lineas = [f"• {v['destino']}" for v in viajes]
    etiqueta = "🇲🇽 *Viajes Nacionales disponibles:*" if tipo == "nacional" else "🌎 *Viajes Internacionales disponibles:*"
    return (
        f"{etiqueta}\n\n"
        + "\n".join(lineas)
        + "\n\n¿Cuál te llama la atención? Dime el destino y te mando precio, fechas y todo lo que incluye. 😊\n"
        "¿Viste algo en nuestras redes que no está aquí? ¡También pregúntame! 📲"
    )


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
    return "\n\n---\n\n".join(_formatear_viaje(v) for v in _leer_viajes())


# ─── Métodos de pago ──────────────────────────────────────────────────────────

METODOS_PAGO = """
💳 *Métodos de pago — Puebla Travel Trips*

• Transferencia bancaria / SPEI
• Tarjeta de crédito o débito (3 y 6 meses sin intereses)
• Efectivo en oficina

📌 *Política de apartado:*
Se requiere un anticipo del 30% para confirmar tu lugar.
El 70% restante se liquida 21 días antes de la salida.

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
📍 *Visítanos en nuestras oficinas  de Puebla Travel Trips*

🗺️ Encuéntranos aquí:
https://maps.app.goo.gl/98v6GQ9bNC1ygsTS9

⏰ *Horario de atención:*
Lunes a Viernes: 9:00 am – 7:00 pm
Sábado: 9:00 am – 2:00 pm

¡Te esperamos con gusto! ✈️
"""
