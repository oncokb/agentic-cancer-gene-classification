FROM python:3.12-slim

WORKDIR /app

# Optional local-agent CLIs for development-only local backend mode.
# Build with: docker build --build-arg INSTALL_LOCAL_AGENTS=true ...
ARG INSTALL_LOCAL_AGENTS=false
RUN if [ "$INSTALL_LOCAL_AGENTS" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends nodejs npm && \
        npm install -g @openai/codex @anthropic-ai/claude-code && \
        npm cache clean --force && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Copy dependency spec first for layer caching
COPY pyproject.toml ./

# Install runtime dependencies straight from pyproject.toml, in their own
# layer, so a source-only change below doesn't reinstall every dependency.
# tomllib is stdlib on this Python version, so no extra parser is needed.
RUN pip install --no-cache-dir hatchling && \
    python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; open('requirements.txt', 'w').write('\n'.join(deps))" && \
    pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY benchmarks/ ./benchmarks/

# Install the project itself (console script + package metadata) against
# the dependencies already installed above, so pyproject.toml stays the
# single source of truth for what this image needs.
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

# Non-root user for K8s security context
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4).read()" || exit 1

CMD ["ddtrace-run", "python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
