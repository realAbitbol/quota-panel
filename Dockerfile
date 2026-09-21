# Pinned by digest rather than by tag: `python:3.12-alpine` is mutable, so two builds of the same
# commit could ship different interpreters — and the interpreter's patch level is published in
# the Server header of every response. This is the multi-arch index digest (verified:
# content-type application/vnd.oci.image.index.v1+json), so it still resolves per platform.
# To bump: docker buildx imagetools inspect python:3.12-alpine
FROM python:3.12-alpine@sha256:c4634f578a412db396771b61b064c6e546c9d6414c7fb5b1b05d5871f1885f7b

# One dependency, for one optional feature: Pillow shrinks a configured background
# image to 4K and re-encodes it as WebP. Without Pillow the panel still runs — the image
# is served exactly as downloaded. Nothing else is pip-installed.
WORKDIR /app

COPY app.py /app/app.py
COPY providers_balance.py /app/providers_balance.py
COPY static /app/static

# Pillow is pinned: the suite asserts exact values of what the re-encoder produced, so an
# unpinned Pillow would let a green build turn red — or silently change what "shrunk" means —
# with no commit behind it.
RUN pip install --no-cache-dir pillow==12.3.0 \
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
