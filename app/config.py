from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from pydantic import SecretStr
from urllib.parse import quote


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "ragdb"
    postgres_user: str = "rag"
    postgres_password:  SecretStr

    # 只读数据库
    readonly_user: str = "nw_readonly"
    readonly_password:  SecretStr

    # 模型
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"

    ollama_base_url: str = "http://localhost:11434"
    embed_model: str = "bge-m3"
    embed_dim: int = 1024

    rerank_backend: str = "llm"          # llm / bge / none
    rerank_top_n: int = 5

    # 检索
    vector_top_k: int = 30
    keyword_top_k: int = 30
    rrf_k: int = 60
    retrieve_top_k: int = 6
    grade_threshold: float = 0.35

    # 其他
    upload_dir: str = "./data/uploads"
    max_retry: int = 2

    # @property
    # def dsn(self) -> str:
    #     return (
    #         f"postgresql://{self.postgres_user}:{self.postgres_password}"
    #         f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    #     )
    @property
    def dsn(self) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(
        self.postgres_password.get_secret_value(),
        safe="",
        )

        return (
            f"postgresql://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    
    # @property
    # def readonly_dsn(self) -> str:
    #     return (
    #         f"postgresql://{self.readonly_user}:{self.readonly_password}"
    #         f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    #     )
    
    @property
    def readonly_dsn(self) -> str:
        user = quote(self.readonly_user, safe="")
        password = quote(
        self.readonly_password.get_secret_value(),
        safe="",
        )

        return (
        f"postgresql://{user}:{password}"
        f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )



@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
