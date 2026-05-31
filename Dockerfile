FROM python:3.12-slim

# ffmpeg: necessário para converter vídeos para formato quadrado (video note)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py web.py main.py db.py ./
COPY templates/ templates/
COPY config.example.json ./
COPY messages.json ./messages.default.json

RUN mkdir -p /data/uploads

ENV DATA_DIR=/data

# /data é o volume persistente do Railway (railway.toml). Uploads vão para
# /data/uploads e sobrevivem a redeploys/reinicializações.

EXPOSE 8080

CMD ["python", "main.py"]
