# syntax=docker/dockerfile:1.7

# Tofu container targets. The digest pins the multi-platform Python 3.12 slim
# index; dependency updates are controlled separately by the committed uv.lock.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

FROM ${PYTHON_IMAGE} AS python-dependencies
# `tofu-repo-kernel`'s setup.py builds its Rust helpers with `cargo build`, and
# its gix discovery crate needs Rust >= 1.88; Debian's rustc is older. Install
# the toolchain the native release builds pin (build-desktop.yml), through a
# checksum-verified rustup-init.
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(dpkg --print-architecture)" in \
         amd64) rust_host=x86_64-unknown-linux-gnu; \
                rustup_sha256=b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55 ;; \
         arm64) rust_host=aarch64-unknown-linux-gnu; \
                rustup_sha256=9f608c142e0e38a78957d0661ba1dfaf0b6fb37184cfaa4b9a0aa57af6184282 ;; \
         *) echo "unsupported build architecture" >&2; exit 1 ;; \
       esac \
    && curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init \
       "https://static.rust-lang.org/rustup/archive/1.29.3/${rust_host}/rustup-init" \
    && echo "${rustup_sha256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain 1.96.0 \
    && rm /tmp/rustup-init \
    && cargo --version
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
# Workspace members are path distributions in uv.lock. A frozen sync fails
# with "Distribution not found at file:///build/packages/..." unless those
# trees exist before uv reads the lock.
COPY packages/tofu-search packages/tofu-search
COPY plugins/tofu-trading plugins/tofu-trading
COPY packages/tofu-repo-kernel packages/tofu-repo-kernel
# This stage only prepares dependencies; the runtime images above stay compile-free.
RUN python -m pip install --no-cache-dir uv==0.13.5 \
    && UV_PROJECT_ENVIRONMENT=/opt/tofu-api \
       UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
       uv sync --frozen --no-dev --no-install-project --extra app --extra scale \
    && UV_PROJECT_ENVIRONMENT=/opt/tofu-worker \
       UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
       uv sync --frozen --no-dev --no-install-project \
       --extra app --extra scale --extra worker \
    && UV_PROJECT_ENVIRONMENT=/opt/tofu-agent \
       UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
       uv sync --frozen --no-dev --no-install-project \
    && uv build --wheel --out-dir /native-wheelhouse packages/tofu-repo-kernel \
    && for environment in /opt/tofu-api /opt/tofu-worker /opt/tofu-agent; do \
         uv pip install --python "$environment/bin/python" --no-deps \
           /native-wheelhouse/tofu_repo_kernel-*.whl || exit 1; \
         "$environment/bin/python" -m tofu_repo_kernel || exit 1; \
       done

# Build and install the public wheel into the dependency-only environment.
# This stage sees the source; the final agent image does not.
FROM python-dependencies AS agent-builder
COPY . .
RUN uv build --wheel --out-dir /wheelhouse \
    && uv pip install --python /opt/tofu-agent/bin/python --no-deps \
       /wheelhouse/tofu_agent-*.whl

FROM ${PYTHON_IMAGE} AS runtime-base
ARG TOFU_UID=10001
ARG TOFU_GID=10001
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini libgcc-s1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${TOFU_GID}" tofu \
    && useradd --uid "${TOFU_UID}" --gid "${TOFU_GID}" \
       --create-home --shell /usr/sbin/nologin tofu
WORKDIR /app
COPY --chown=tofu:tofu . .
RUN mkdir -p /app/data/backups /app/logs /app/uploads \
    && chown -R tofu:tofu /app/data /app/logs /app/uploads
ENV PORT=15000 \
    BIND_HOST=0.0.0.0 \
    MALLOC_ARENA_MAX=8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOFU_SERVER_WORKER=1 \
    TOFU_MANAGED_BY=docker
EXPOSE 15000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:15000/health/live || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]

# API target: no compiler, PostgreSQL server, Playwright package, or browser.
FROM runtime-base AS api
COPY --from=python-dependencies --chown=tofu:tofu /opt/tofu-api /opt/tofu-api
ENV PATH=/opt/tofu-api/bin:$PATH
USER tofu:tofu

# Worker target: browser/tool runtimes are isolated from the serving image.
# Docker Compose targets this stage so the low-cost personal `all` role keeps
# the browser features historically shipped by the standalone image.
FROM runtime-base AS worker
COPY --from=python-dependencies --chown=tofu:tofu \
    /opt/tofu-worker /opt/tofu-worker
ENV PATH=/opt/tofu-worker/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN /opt/tofu-worker/bin/python -m playwright install chromium --with-deps --only-shell \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R tofu:tofu /opt/ms-playwright
USER tofu:tofu

# Public headless runtime: wheel + agent dependencies only. It contains no
# repository checkout, full Tofu application bundle, database driver, or app data.
FROM ${PYTHON_IMAGE} AS agent
ARG TOFU_UID=10001
ARG TOFU_GID=10001
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini libgcc-s1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${TOFU_GID}" tofu \
    && useradd --uid "${TOFU_UID}" --gid "${TOFU_GID}" \
       --create-home --shell /usr/sbin/nologin tofu \
    && mkdir -p /app/logs /workspace /home/tofu/.config/tofu-agent \
    && chown -R tofu:tofu /app /workspace /home/tofu/.config
COPY --from=agent-builder --chown=tofu:tofu /opt/tofu-agent /opt/tofu-agent
WORKDIR /workspace
ENV PATH=/opt/tofu-agent/bin:$PATH \
    TOFU_AGENT_HOST=0.0.0.0 \
    TOFU_AGENT_PORT=15001 \
    TOFU_AGENT_CONFIG_PATH=/home/tofu/.config/tofu-agent/provider.json \
    MALLOC_ARENA_MAX=8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOFU_MANAGED_BY=docker
EXPOSE 15001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:15001/health/live || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tofu-agent", "serve"]
USER tofu:tofu
