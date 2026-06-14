FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INVENTORY_HOST=0.0.0.0 \
    INVENTORY_PORT=8081 \
    INVENTORY_DB=/data/app.db

WORKDIR /app
COPY app ./app
COPY static ./static

EXPOSE 8081
CMD ["python", "-m", "app.server"]
