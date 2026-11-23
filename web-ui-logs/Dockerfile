FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py tailer.py ./
COPY static ./static

ENV LOG_FILE=/downloads/logs/fb-dl.log
ENV ALBUMS_FILE=/config/albums.txt
EXPOSE 8081
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
