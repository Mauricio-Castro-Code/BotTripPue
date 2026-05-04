"""
Convierte assets/Nota.xls → assets/Nota.xlsx usando LibreOffice.
Corre esto UNA SOLA VEZ despues de instalar LibreOffice:

    python scripts/setup_excel_template.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
ASSETS  = Path(__file__).resolve().parent.parent / "assets"
XLS_SRC = ASSETS / "Nota.xls"
XLSX_OUT = ASSETS / "Nota.xlsx"


def main() -> None:
    if not SOFFICE.exists():
        print("ERROR: LibreOffice no encontrado en", SOFFICE)
        print("Instala con:  brew install --cask libreoffice")
        sys.exit(1)

    if not XLS_SRC.exists():
        print("ERROR: No se encontro", XLS_SRC)
        sys.exit(1)

    print(f"Convirtiendo {XLS_SRC.name} → xlsx ...")
    result = subprocess.run(
        [str(SOFFICE), "--headless", "--convert-to", "xlsx",
         "--outdir", str(ASSETS), str(XLS_SRC)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print("ERROR en LibreOffice:\n", result.stderr)
        sys.exit(1)

    if XLSX_OUT.exists():
        print(f"Listo: {XLSX_OUT}")
    else:
        print("ERROR: No se genero el archivo .xlsx")
        sys.exit(1)


if __name__ == "__main__":
    main()
