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

RUN mkdir -p /data /app/static/uploads

ENV DATA_DIR=/data

# uploads persistidos via volume para sobreviver a reinicializações
VOLUME ["/app/static/uploads"]

EXPOSE 8080

CMD ["python", "main.py"]
