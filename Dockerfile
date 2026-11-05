FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

# Editable on purpose: the router finds .cache/ and data/ relative to its source
# files, so they must stay under /app rather than move to site-packages.
RUN pip install --no-cache-dir -e ".[web]" \
    && useradd --create-home app \
    && mkdir .cache \
    && chown app .cache
USER app

EXPOSE 8000
CMD ["uvicorn", "router.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
