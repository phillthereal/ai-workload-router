FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

# AWR_HOME is where the router keeps data/ (run log, daily spend store) and .cache/.
# Mount a volume here to keep them across restarts.
ENV AWR_HOME=/var/lib/awr
RUN pip install --no-cache-dir ".[web]" \
    && useradd --create-home app \
    && mkdir -p "$AWR_HOME/data" "$AWR_HOME/.cache" \
    && chown -R app "$AWR_HOME"
USER app

EXPOSE 8000
CMD ["uvicorn", "router.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
