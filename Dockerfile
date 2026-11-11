FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dashboard.py awc.py battery.py security.py .
COPY assets ./assets
RUN useradd --uid 10001 --create-home dashboard && mkdir /data && chown dashboard:dashboard /data
USER dashboard
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)"
CMD ["python", "dashboard.py"]
