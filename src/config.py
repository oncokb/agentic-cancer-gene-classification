from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    oncokb_api_token: str = ""
    ncbi_api_key: str = ""

    synthesis_model: str = "claude-opus-4-7"
    selection_model: str = "claude-haiku-4-5-20251001"
    pubmed_max_results: int = 50
    min_papers_for_strong_association: int = 4
    max_papers_for_synthesis: int = 8
    max_citations_per_annotation: int = 4

    gene_cache_enabled: bool = True
    gene_cache_oncokb_check_days: int = 90
    gene_cache_high_support_days: int = 60
    gene_cache_medium_support_days: int = 30
    gene_cache_low_support_days: int = 14
    gene_cache_high_support_threshold: float = 0.8
    gene_cache_medium_support_threshold: float = 0.5
    gene_cache_freshness_pmids: int = 20

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "agcg"
    mysql_password: str = ""
    mysql_database: str = "agcg"

    log_level: str = "INFO"


settings = Settings()
