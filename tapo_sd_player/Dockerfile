FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytapo==3.4.30 aiohttp==3.14.5
COPY app.py index.html /app/
CMD ["python", "-I", "/app/app.py"]
