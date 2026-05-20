from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=[".env", "../.env"], env_prefix="PHOENIX_UI_", extra="ignore")
    port: int = 8101


class TopLevel(BaseSettings):
    model_config = SettingsConfigDict(env_file=[".env", "../.env"], extra="ignore")
    phoenix_brain_url: str = "http://localhost:8102"
    phoenix_listener_url: str = "http://localhost:8100"


settings = Settings()
top = TopLevel()
