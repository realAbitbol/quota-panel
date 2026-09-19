FROM python:3.12-alpine

# stdlib only: nothing to pip-install, so the image has no dependency drift and
# nothing to patch beyond the base image itself.
WORKDIR /app

COPY app.py /app/app.py
COPY static /app/static

RUN adduser -D -u 10001 quota \
 && mkdir -p /data /config \
 && chown -R quota:quota /data /config /app
USER quota

ENV PORT=8080 \
    QUOTA_CONFIG=/config/accounts.json \
    QUOTA_DB=/data/quota.db \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "/app/app.py"]
