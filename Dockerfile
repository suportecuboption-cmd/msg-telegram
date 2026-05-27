FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py web.py main.py ./
COPY templates/ templates/
COPY config.example.json ./
COPY messages.json ./messages.default.json

RUN mkdir -p /data

ENV DATA_DIR=/data

EXPOSE 8080

CMD ["python", "main.py"]
