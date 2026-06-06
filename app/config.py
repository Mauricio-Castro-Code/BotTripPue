from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    OPENAI_API_KEY: str
    WHATSAPP_TOKEN: str
    WHATSAPP_VERIFY_TOKEN: str
    WHATSAPP_PHONE_NUMBER_ID: str
    MESSENGER_PAGE_TOKEN: str = ""   # Page Access Token de la página de Facebook
    FACEBOOK_APP_ID: str = ""        # App ID de la app en Meta Developers
    BLOCKED_PHONES: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def blocked_phones_set(self) -> set[str]:
        return {p.strip() for p in self.BLOCKED_PHONES.split(",") if p.strip()}


settings = Settings()
