FROM python:3.12-alpine

# One dependency, for one optional feature: Pillow shrinks a configured background
# image to 4K and re-encodes it as WebP. Without Pillow the panel still runs — the image
# is served exactly as downloaded. Nothing else is pip-installed.
WORKDIR /app

COPY app.py /app/app.py
COPY static /app/static

RUN pip install --no-cache-dir pillow \
 && adduser -D -u 10001 quota \
 && mkdir -p /config \
 && chown -R quota:quota /config /app
USER quota

ENV PORT=8080 \
    QUOTA_CONFIG=/config/accounts.json \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "/app/app.py"]
