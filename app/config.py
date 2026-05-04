from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    OPENAI_API_KEY: str
    WHATSAPP_TOKEN: str
    WHATSAPP_VERIFY_TOKEN: str
    WHATSAPP_PHONE_NUMBER_ID: str
    NEGOCIO_ID: str
    PDF_DIR: str = "pdf"
    LOGO_PATH: str = "assets/logo.png"
    EXCEL_TEMPLATE_PATH: str = "assets/Nota.xlsx"
    LIBREOFFICE_PATH: str = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    # Telefonos para los que el bot NO responde (familiares, internos, etc.)
    # Formato en .env: BLOCKED_PHONES=5212212664376,5215512345678
    BLOCKED_PHONES: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def blocked_phones_set(self) -> set[str]:
        return {p.strip() for p in self.BLOCKED_PHONES.split(",") if p.strip()}


settings = Settings()
