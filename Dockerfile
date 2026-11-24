FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

# Editable on purpose: the router finds .cache/ and data/ relative to its source
# files, so they must stay under /app rather than move to site-packages.
# data/ holds the spend store (data/spend.db) the web app opens at startup.
RUN pip install --no-cache-dir -e ".[web]" \
    && useradd --create-home app \
    && mkdir .cache data \
    && chown app .cache data
USER app

EXPOSE 8000
CMD ["uvicorn", "router.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
