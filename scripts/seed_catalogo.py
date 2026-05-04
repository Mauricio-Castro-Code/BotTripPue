"""
Sincroniza data/catalogo.py con la tabla `inventario` en PostgreSQL.

Uso:
    python scripts/seed_catalogo.py

Por cada producto del catalogo:
  - Si ya existe por nombre_producto (case-insensitive) -> UPDATE precio/stock
  - Si no existe -> INSERT

No borra productos que esten en la BD pero no en el catalogo
(por si tienes agregados manuales — puedes borrarlos con SQL a mano si quieres).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permite correr el script desde la raiz del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Inventario  # noqa: E402
from data.catalogo import CATALOGO  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        nuevos = 0
        actualizados = 0
        for prod in CATALOGO:
            nombre = prod["nombre"]
            existente = (
                db.query(Inventario)
                .filter(
                    Inventario.negocio_id == settings.NEGOCIO_ID,
                    func.lower(Inventario.nombre_producto) == nombre.lower(),
                )
                .first()
            )
            if existente:
                existente.precio_renta = prod["precio"]
                existente.stock_total = prod["stock"]
                existente.activo = True
                actualizados += 1
                print(f"  [~] {nombre}  (${prod['precio']:.2f}, stock {prod['stock']})")
            else:
                db.add(Inventario(
                    negocio_id=settings.NEGOCIO_ID,
                    nombre_producto=nombre,
                    precio_renta=prod["precio"],
                    stock_total=prod["stock"],
                    activo=True,
                ))
                nuevos += 1
                print(f"  [+] {nombre}  (${prod['precio']:.2f}, stock {prod['stock']})")

        db.commit()
        print(f"\nListo: {nuevos} nuevos, {actualizados} actualizados.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
