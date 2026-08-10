FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY daemon /opt/heliox/daemon

RUN python -m pip install "/opt/heliox/daemon[dev,wasm,documents,browser,automation,network,ssh]" \
    && rm -rf /opt/heliox/daemon

WORKDIR /workspace

# The runtime command is always supplied by EvolutionHarness. The container
# receives no network, no host credentials, a read-only root filesystem, and
# only one disposable candidate worktree mount.
CMD ["python", "--version"]
