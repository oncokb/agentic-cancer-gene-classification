from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_sdk_provider: str = "anthropic"
    anthropic_api_key: str = ""
    oncokb_api_token: str = ""
    ncbi_api_key: str = ""

    synthesis_model: str = "claude-opus-4-7"
    synthesis_fast_model: str = "claude-haiku-4-5-20251001"
    synthesis_model_escalation: bool = True
    synthesis_escalation_min_support_score: float = 0.5
    synthesis_escalation_min_citations: int = 1
    synthesis_escalation_tier2: bool = True
    core_synthesis_max_tokens: int = 640
    core_synthesis_abstract_chars: int = 500
    core_synthesis_max_papers: int = 6
    core_synthesis_escalation_min_support_score: float = 0.0
    core_synthesis_escalation_tier2: bool = False
    selection_model: str = "claude-haiku-4-5-20251001"
    bedrock_synthesis_model: str = ""
    bedrock_synthesis_fast_model: str = ""
    bedrock_selection_model: str = ""
    bedrock_aws_access_key_id: str = ""
    bedrock_aws_secret_access_key: str = ""
    bedrock_aws_session_token: str = ""
    bedrock_aws_default_region: str = ""
    bedrock_aws_profile: str = ""
    bedrock_reverse_proxy: str = ""
    aws_region: str = ""
    aws_default_region: str = ""
    aws_profile: str = ""
    acgc_dev_mode: bool = False
    pubmed_max_results: int = 50
    fusion_evidence_max_results: int = 20
    fusion_evidence_cache_ttl_seconds: int = 604800
    fusion_evidence_concurrency: int = 2
    min_papers_for_strong_association: int = 4
    max_papers_for_synthesis: int = 8
    max_citations_per_annotation: int = 4
    annotation_gene_concurrency: int = 3
    llm_concurrency: int = 2
    pubmed_staged_retrieval: bool = True
    selection_llm_threshold: int = 24
    annotation_job_ttl_seconds: int = 3600

    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl_seconds: int = 86400

    # Redis Sentinel (master/replica with automatic failover). When enabled,
    # takes over from redis_url entirely for cache connections.
    redis_sentinel_enabled: bool = False
    redis_sentinel_hosts: str = ""  # comma-separated host:port, e.g. "sentinel-0:26379,sentinel-1:26379"
    redis_sentinel_master_set: str = "mymaster"
    redis_sentinel_password: str = ""

    gene_cache_enabled: bool = True
    gene_cache_oncokb_check_days: int = 90
    gene_cache_high_support_days: int = 60
    gene_cache_medium_support_days: int = 30
    gene_cache_low_support_days: int = 14
    gene_cache_final_annotation_days: int = 180
    gene_cache_high_support_threshold: float = 0.8
    gene_cache_medium_support_threshold: float = 0.5
    gene_cache_freshness_pmids: int = 20

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "acgc"
    mysql_password: str = ""
    mysql_database: str = "acgc"

    # Alternative to MYSQL_HOST/MYSQL_PORT: a JDBC-style connection string
    # (e.g. "jdbc:mysql://host:3306"), as used by this org's shared RDS
    # secrets. When set, host/port are parsed from it and take precedence
    # over MYSQL_HOST/MYSQL_PORT. DB_USERNAME/DB_PASSWORD similarly take
    # precedence over MYSQL_USER/MYSQL_PASSWORD when set. MYSQL_DATABASE
    # is unaffected — a JDBC URL identifies a server, not a database.
    db_url: str = ""
    db_username: str = ""
    db_password: str = ""

    fusion_annotation_api_enabled: bool = False
    fusion_annotation_api_base_url: str = ""
    fusion_annotation_api_timeout_seconds: float = 15.0
    fusion_context_cache_ttl_seconds: int = 604800

    log_level: str = "INFO"

    datadog_metrics_enabled: bool = False
    datadog_metrics_namespace: str = "acgc"
    datadog_statsd_host: str = "127.0.0.1"
    datadog_statsd_port: int = 8125
    datadog_user_id_header: str = "x-user-id"


settings = Settings()
