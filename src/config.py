from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str
    oncokb_api_token: str = ""
    ncbi_api_key: str = ""

    synthesis_model: str = "claude-opus-4-7"
    selection_model: str = "claude-haiku-4-5-20251001"
    pubmed_max_results: int = 50
    min_papers_for_strong_association: int = 4
    max_papers_for_synthesis: int = 8

    log_level: str = "INFO"


settings = Settings()
