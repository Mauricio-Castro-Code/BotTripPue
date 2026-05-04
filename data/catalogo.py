"""
Catalogo de productos de Alquiladora Crystal — FUENTE DE VERDAD UNICA.

Aqui defines cada producto con:
  - nombre:   como queda escrito en la cotizacion/PDF  (ej. "Silla Acojinada")
  - alias:    formas coloquiales en las que los clientes lo pueden pedir
              (la IA mapea cualquiera de estas -> nombre oficial)
  - precio:   precio unitario de renta en pesos
  - stock:    cantidad disponible
  - categoria: grupo para reportes/consultas ("sillas", "mesas", "manteleria", etc.)

Despues de editar este archivo, corre:
    python scripts/seed_catalogo.py

Eso sincroniza la tabla `inventario` en la BD con este catalogo.
Los sinonimos se leen EN VIVO desde aqui — no necesitan reinicio aparte del uvicorn.
"""
from __future__ import annotations


CATALOGO: list[dict] = [
    # ─── SILLAS ──────────────────────────────────────────────────────────────
    {
        "nombre":    "Silla Acojinada",
        "alias":     ["silla acolchada", "silla con cojin", "silla con cojín",
                      "silla cromada", "silla barata",
                      "silla economica", "silla económica"],
        "precio":    11.00,
        "stock":     500,
        "categoria": "sillas",
    },
    {
        "nombre":    "Silla Tiffany",
        "alias":     ["tiffany", "silla tifany", "silla elegante", "tifany"],
        "precio":    35.00,
        "stock":     600,
        "categoria": "sillas",
        # Si el cliente no especifica color, el bot le preguntara cual quiere.
        # Cada color lista sus abreviaturas/formas coloquiales.
        "colores": {
            "Blanca":    ["blanca", "bca", "blanco", "bco", "bca."],
            "Chocolate": ["chocolate", "cafe", "café", "madera", "chocolat", "choco"],
            "Plata":     ["plata", "plateada", "plateado", "gris", "plt"],
        },
    },
    {
        "nombre":    "Silla Crossback",
        "alias":     ["crossback", "silla de madera", "silla rústica"],
        "precio":    50.00,
        "stock":     100,
        "categoria": "sillas",
    },

    # ─── MESAS ───────────────────────────────────────────────────────────────
    {
        "nombre":    "MesaTablon 10 personas",
        "alias":     ["mesa larga", "mesa rectangular", "mesa tablón", "mesa banquete"],
        "precio":    35.00,
        "stock":     60,
        "categoria": "mesas",
    },
    {
        "nombre":    "Mesa Redonda 10 personas",
        "alias":     ["mesa redonda", "mesa circular"],
        "precio":    35.00,
        "stock":     40,
        "categoria": "mesas",
    },
        {
        "nombre":    "Mesa Vintage",
        "alias":     ["mesa vintage","mesas vintage", "mesa gris", "mesa plata"],
        "precio":    550.00,
        "stock":     40,
        "categoria": "mesas",
    },
        {
        "nombre":    "Mesa Organica",
        "alias":     ["mesa madera", "mesas madera", "mesa cafe", "mesa elegante"],
        "precio":    650.00,
        "stock":     40,
        "categoria": "mesas",
    },

    # ─── MANTELERIA ──────────────────────────────────────────────────────────
    {
        "nombre":    "Mantel Tablon",
        "alias":     ["mantel largo", "mantel rectangular", "mantel de mesa tablon",
                      "mantel de tablón"],
        "precio":    40.00,
        "stock":     120,
        "categoria": "manteleria",
    },
    {
        "nombre":    "Mantel Redondo",
        "alias":     ["mantel circular", "mantel de mesa redonda","mantel mesa cuadrada"],
        "precio":    40.00,
        "stock":     80,
        "categoria": "manteleria",
    },
    # ─── CARPAS ──────────────────────────────────────────────────────────
    {
        "nombre":    "Carpa 10x10",
        "alias":     ["carpa 10x10", "carpa grande", "carpa evento","carpa para 100 personas","toldo grande","lona grande"],
        "precio":    4500.00,
        "stock":     20,
        "categoria": "carpas",
    },
    {
        "nombre":    "Carpa 6x6",
        "alias":     ["carpa 6x6", "carpa mediana", "carpa evento","carpa para 50 personas","toldo mediano","lona mediana"],
        "precio":    1600.00,
        "stock":     20,
        "categoria": "carpas",
    },
        {
        "nombre":    "Carpa 6x3",
        "alias":     ["carpa 6x3", "carpa pequeña", "carpa evento","carpa para 30 personas","toldo pequeño","lona pequeña"],
        "precio":    1200.00,
        "stock":     20,
        "categoria": "carpas",
    },
    # ─── AGREGA AQUI TUS PRODUCTOS NUEVOS ────────────────────────────────────
    # Plantilla:
    # {
    #     "nombre":    "Nombre Oficial",
    #     "alias":     ["como lo dicen los clientes", "otro sinonimo"],
    #     "precio":    0.00,
    #     "stock":     0,
    #     "categoria": "sillas",   # sillas | mesas | manteleria | carpas | loza | otros
    # },
]


def construir_sinonimos() -> dict[str, str]:
    """Expone un dict {alias_lower: nombre_oficial_lower} para buscar_producto()."""
    mapa: dict[str, str] = {}
    for prod in CATALOGO:
        nombre_lower = prod["nombre"].lower()
        for alias in prod.get("alias", []):
            mapa[alias.lower()] = nombre_lower
    return mapa


def producto_por_nombre(nombre: str) -> dict | None:
    """Busca un producto del catalogo por nombre oficial (case-insensitive)."""
    n = nombre.lower()
    for prod in CATALOGO:
        if prod["nombre"].lower() == n:
            return prod
    return None


def detectar_color_en_texto(texto: str, colores_dict: dict[str, list[str]]) -> str | None:
    """
    Dado un texto ('sillas tiffany bca') y un dict {color: [aliases]},
    devuelve el color oficial encontrado ('Blanca') o None.
    """
    t = f" {texto.lower()} "
    for color_oficial, aliases in colores_dict.items():
        for alias in aliases:
            if f" {alias} " in t or t.strip().endswith(alias):
                return color_oficial
    return None


def tiene_variantes_color(descripcion: str) -> tuple[str, dict] | None:
    """
    Si la descripcion matchea un producto con variantes de color, devuelve
    (nombre_oficial, colores_dict). Si no, None.
    """
    desc = descripcion.lower()
    for prod in CATALOGO:
        if "colores" not in prod:
            continue
        if prod["nombre"].lower() in desc:
            return prod["nombre"], prod["colores"]
        for alias in prod.get("alias", []):
            if alias.lower() in desc:
                return prod["nombre"], prod["colores"]
    return None
