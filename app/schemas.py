"""
Esquemas Pydantic para validar el payload entrante de Meta/WhatsApp
y para estructurar los datos de renta extraídos por la IA.
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


# ─── Payload de WhatsApp (Meta) ───────────────────────────────────────────────

class WaTextBody(BaseModel):
    body: str


class WaMessage(BaseModel):
    # Meta envía "from" (palabra reservada en Python) — alias resuelve el conflicto
    from_: str | None = Field(default=None, alias="from")
    id: str | None = None
    timestamp: str | None = None
    text: WaTextBody | None = None
    type: str | None = None

    model_config = {"populate_by_name": True, "extra": "allow"}


class WaContact(BaseModel):
    profile: dict[str, Any] | None = None
    wa_id: str | None = None


class WaMetadata(BaseModel):
    display_phone_number: str | None = None
    phone_number_id: str | None = None


class WaValue(BaseModel):
    messaging_product: str | None = None
    metadata: WaMetadata | None = None
    contacts: list[WaContact] | None = None
    messages: list[WaMessage] | None = None

    model_config = {"extra": "allow"}


class WaChange(BaseModel):
    value: WaValue | None = None
    field: str | None = None


class WaEntry(BaseModel):
    id: str | None = None
    changes: list[WaChange] | None = None


class WhatsAppWebhookPayload(BaseModel):
    object: str | None = None
    entry: list[WaEntry] | None = None


# ─── Datos de renta extraídos por la IA ───────────────────────────────────────

_KEYWORDS_REQUIEREN_COLOR = ("tiffany", "tifany")


class ItemEquipo(BaseModel):
    cantidad: int
    descripcion: str


class DatosRenta(BaseModel):
    """
    Acumula la información de la cotización durante la conversación.
    Todos los campos son opcionales porque se van llenando en múltiples mensajes.
    """
    nombre: str | None = None
    domicilio: str | None = None
    colonia: str | None = None
    referencia: str | None = None
    fecha_entrega: str | None = None       # YYYY-MM-DD
    hora_entrega: str | None = None        # HH:MM (24h)
    fecha_evento: str | None = None        # YYYY-MM-DD
    fecha_recoleccion: str | None = None   # YYYY-MM-DD
    equipo: list[ItemEquipo] | None = None
    instrucciones: str | None = None
    tipo_entrega: str | None = None        # "domicilio" o "recoger"
    maps_link: str | None = None           # Link de Google Maps (opcional)
    requiere_factura: bool | None = None   # Si requiere factura → se calcula IVA 16%
    mantel_color: str | None = None        # Color del mantel (si renta manteleria)
    silla_color:  str | None = None        # Color de la silla Tiffany (si renta Tiffany)
    manteleria_consultada: bool = False    # Flag — ya se pregunto/decidio sobre manteleria

    def fusionar(self, nuevos: "DatosRenta") -> "DatosRenta":
        """
        Devuelve una nueva instancia fusionando los campos nuevos con los existentes.
        'equipo' siempre se reemplaza si el cliente proporcionó nuevos artículos.
        """
        datos = self.model_dump()
        for campo, valor in nuevos.model_dump(exclude_none=True).items():
            if campo == "equipo":
                datos["equipo"] = valor
            elif not datos.get(campo):
                datos[campo] = valor
        return DatosRenta.model_validate(datos)

    @property
    def campos_faltantes(self) -> list[str]:
        """Devuelve los campos obligatorios que aún faltan en orden de prioridad."""
        faltantes = []
        if not self.nombre:
            faltantes.append("nombre")
        if not self.equipo:
            faltantes.append("equipo")
        if self.equipo and not self.mantel_color:
            if any("mantel" in (it.descripcion or "").lower() for it in self.equipo):
                faltantes.append("mantel_color")
        # Si hay Silla Tiffany (o variante) y no se definio color → faltante
        if self.equipo and not self.silla_color:
            if any(
                any(kw in (it.descripcion or "").lower() for kw in _KEYWORDS_REQUIEREN_COLOR)
                for it in self.equipo
            ):
                faltantes.append("silla_color")
        if not self.fecha_evento:
            faltantes.append("fecha_evento")
        if not self.tipo_entrega:
            faltantes.append("tipo_entrega")
        # Domicilio y colonia siempre — necesarios para el registro del cliente
        if not self.domicilio:
            faltantes.append("domicilio")
        if not self.colonia:
            faltantes.append("colonia")
        # Referencia solo si el equipo va a domicilio (la usa el chofer)
        if self.tipo_entrega == "domicilio" and not self.referencia:
            faltantes.append("referencia")
        if not self.fecha_entrega:
            faltantes.append("fecha_entrega")
        if not self.hora_entrega:
            faltantes.append("hora_entrega")
        if not self.fecha_recoleccion:
            faltantes.append("fecha_recoleccion")
        if self.requiere_factura is None:
            faltantes.append("requiere_factura")
        return faltantes

    @property
    def completo(self) -> bool:
        return len(self.campos_faltantes) == 0
