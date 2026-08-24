python_sources(
    name="lib",
    sources=["order_api/**/*.py"],
)

python_tests(
    name="tests",
    # `!tests/__init__.py`: it's a namespace marker, not a test — including it
    # makes Pants generate a `python_test` target for it that pytest then fails
    # with "no tests collected".
    sources=["tests/**/*.py", "!tests/__init__.py"],
    dependencies=[":lib"],
)

pex_binary(
    name="bin",
    # Declares this target as something CI ships. `pants --tag=deployable
    # package ::` selects on it, so a service is packaged the moment it says
    # so itself — no list in the pipeline to keep in sync.
    tags=["deployable"],
    entry_point="order_api.main:main",
    dependencies=[":lib"],
    # Land in dist/ under a predictable name so dist/ can be used directly as
    # the Buildah build context and the Dockerfile can COPY by name. Default
    # would be dist/services.order-api/bin.pex.
    output_path="order-api.pex",
)
