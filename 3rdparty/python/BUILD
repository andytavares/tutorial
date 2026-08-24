python_requirements(
    name="reqs",
    source="requirements.txt",
)

# The cluster's real platform: linux/aarch64, CPython 3.13. Generated with
#   docker run --rm python:3.13-slim sh -c 'pip install pex && pex3 interpreter inspect --markers --tags'
#
# Every pex_binary MUST reference this via `complete_platforms`. Without it,
# `pants package` on a macOS laptop resolves macOS wheels — grpcio, pydantic
# core, uvloop and watchfiles are all native — and produces a PEX that builds
# cleanly, passes every check, and then dies on import inside a linux
# container. The failure is at runtime, in the cluster, far from the cause.
file(
    name="linux-platform",
    source="linux.json",
)
