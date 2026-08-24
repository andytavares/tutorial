"""Config from the environment, validated once at import.

pydantic-settings' `BaseSettings` is the approach FastAPI documents for this
(https://fastapi.tiangolo.com/advanced/settings/, and
https://docs.pydantic.dev/latest/concepts/pydantic_settings/). A field with no
default is required: if it is missing the process refuses to start, and pydantic
reports *every* missing or malformed variable at once rather than only the first.
Types are declared, not cast by hand.

Environment variable names match field names case-insensitively, so `kafka_topic`
reads `KAFKA_TOPIC`. Where the variable a field must read is not the upper-cased
field name, `validation_alias` pins the real name.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    kafka_brokers: str
    kafka_topic: str = "orders"

    s3_bucket: str
    # boto3 owns the name AWS_DEFAULT_REGION, so the field cannot simply be
    # called `default_region`; the alias binds the field to the variable boto3
    # and every AWS tool already expect.
    aws_region: str = Field(default="us-east-1", validation_alias="AWS_DEFAULT_REGION")
    aws_endpoint_url: str | None = None

    # Injected by External Secrets Operator from OpenBao. See §7. The alias keeps
    # the variable namespaced to this service while the field stays generic.
    signing_key: str = Field(validation_alias="ORDER_SIGNING_KEY")

    service_version: str = "dev"


# pydantic's metaclass is a PEP 681 `dataclass_transform`, so mypy synthesises an
# `__init__` taking every field and reports the three required ones as missing
# arguments. They are not passed as arguments — BaseSettings reads them from the
# environment, which is the entire point of the class.
settings = Settings()  # type: ignore[call-arg]
