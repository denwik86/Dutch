FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Папка для хранения прогресса (монтируй как volume)
RUN mkdir -p /app/data
ENV DATA_FILE=/app/data/progress.json

CMD ["python", "bot.py"]
