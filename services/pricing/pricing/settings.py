from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Config from the environment.

    pydantic-settings does the parsing, casting and validation, and reports
    every bad or missing variable at once instead of only the first.
    """

    # Every variable this service reads is prefixed, so the field names stay
    # short and nothing here can collide with an unprefixed cluster variable.
    model_config = SettingsConfigDict(env_prefix="PRICING_")

    grpc_port: int = 50051
    http_port: int = 9090
    version: str = "v1"


settings = Settings()
