python_sources(
    name="lib",
    sources=["order_api/**/*.py"],
)

python_sources(
    name="tools",
    sources=["openapi_dump.py"],
    dependencies=[":lib"],
)

pex_binary(
    name="dump-openapi",
    entry_point="openapi_dump.py",
    dependencies=[":tools"],
)

resource(
    name="openapi-spec",
    source="openapi.json",
)

python_tests(
    name="tests",
    # `!tests/__init__.py`: it's a namespace marker, not a test — including it
    # makes Pants generate a `python_test` target for it that pytest then fails
    # with "no tests collected".
    sources=["tests/**/*.py", "!tests/__init__.py"],
    # `:openapi-spec` is a resource, not code, so nothing infers it — the drift
    # test reads it off disk by path.
    dependencies=[":lib", ":openapi-spec"],
)

pex_binary(
    name="bin",
    # Declares this target as something CI ships. `pants --tag=deployable
    # package ::` selects on it, so a service is packaged the moment it says
    # so itself — no list in the pipeline to keep in sync.
    tags=["deployable"],
    entry_point="order_api.main:main",
    dependencies=[":lib"],
    # The cluster runs linux/aarch64, not this macOS laptop. Without this,
    # `pants package` resolves macOS wheels for native deps (grpcio,
    # pydantic-core, uvloop, watchfiles) that import-fail in the container.
    complete_platforms=["3rdparty/python:linux-platform"],
    # Land in dist/ under a predictable name so dist/ can be used
    # directly as the Buildah build context and the Dockerfile can COPY by
    # name. Default would be dist/services.<name>/bin.pex.
    output_path="order-api.pex",
)
