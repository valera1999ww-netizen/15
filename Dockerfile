FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot.py /app/bot.py
COPY .env.example /app/.env.example
COPY README.md /app/README.md
COPY Dockerfile /app/Dockerfile
COPY render.yaml /app/render.yaml

RUN mkdir -p /app/data

CMD ["python", "bot.py"]
