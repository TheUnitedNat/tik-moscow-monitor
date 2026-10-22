FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mgik_api.py regions.py storage.py scanner.py telegram_bot.py ./
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
VOLUME ["/app/data"]
CMD ["python", "telegram_bot.py"]
