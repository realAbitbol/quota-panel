# Pinned by digest rather than by tag: `python:3.12-alpine` is mutable, so two builds of the same
# commit could ship different interpreters — and the interpreter's patch level is published in
# the Server header of every response. This is the multi-arch index digest (verified:
# content-type application/vnd.oci.image.index.v1+json), so it still resolves per platform.
# To bump: docker buildx imagetools inspect python:3.12-alpine
FROM python:3.12-alpine@sha256:c4634f578a412db396771b61b064c6e546c9d6414c7fb5b1b05d5871f1885f7b

WORKDIR /app

# Everything mutable sits above the COPYs. This layer installs Pillow and creates the runtime
# user; it used to sit below `COPY static`, so editing static/index.html — one of the most
# frequent edits — invalidated it and redownloaded Pillow, twice per multi-arch build.
#
# Pillow is one dependency for one optional feature: it shrinks a configured background image to
# 4K and re-encodes it as WebP. Without Pillow the panel still runs and serves that image exactly
# as downloaded. Nothing else is pip-installed. It is pinned because the suite asserts exact
# values of what the re-encoder produced, so an unpinned Pillow could let a green build turn red
# — or silently change what "shrunk" means — with no commit behind it.
#
# /data exists in the image for the same reason /config does. A named volume mounted at a path
# that is absent from the image is created root-owned and stays root-owned, and this container
# runs as uid 10001 — so a compose user who turned the history layer on and uncommented the volume
# got `/api/quota` reporting "cannot open the history database at /data/quota.db (unable to open
# database file)" and `/api/history` answering 503. The directory created here is what the named
# volume is initialised from (content and ownership), so the writable path ships with the image.
# Only a host bind mount remains the host's to make writable, as the README says.
RUN pip install --no-cache-dir pillow==12.3.0 \
 && adduser -D -u 10001 quota \
 && mkdir -p /config /data \
 && chown -R quota:quota /config /data /app

# Only these files are in the image. `--chown` replaces the blanket `chown -R quota /app` that
# used to run after the copy: the runtime user needs to read these, not to own them.
COPY --chown=quota:quota app.py /app/app.py
COPY --chown=quota:quota providers_balance.py /app/providers_balance.py
# The optional history layer. It has to be in this list like anything else: app.py imports it
# lazily, so an image that left it out would not fail — it would silently serve a panel whose
# history, once enabled, never appears.
COPY --chown=quota:quota history.py /app/history.py
COPY --chown=quota:quota static /app/static
# MIT requires the notice to travel with every copy of the software, and a redistributed image is
# a copy. It has nowhere else to come from: the COPY list is explicit, so a file left out of it
# simply is not in the image.
COPY --chown=quota:quota LICENSE /app/LICENSE

USER quota

ENV PORT=8080 \
    QUOTA_CONFIG=/config/accounts.json \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# --start-period=60s, not 20s: /api/health answers 503 until the first refresh_all finishes, and
# that first poll is sequential over every account at up to QUOTA_HTTP_TIMEOUT each — so 20s
# guaranteed an unhealthy container through its whole warm-up, and delayed anything using
# `depends_on: service_healthy`.
#
# The program below is byte-identical to the one in docker-compose.yml, which overrides this
# instruction for compose users: two copies of a probe that drift apart is how one of them ends
# up wrong. It checks for an exception rather than a status code, because urlopen returns a
# response only for 2xx and raises for everything else — a `.status==200` test with an `else 1`
# left a branch that can never run, and printed a traceback on every unhealthy probe instead of
# exiting quietly. Measured with this exact program: 200 -> exit 0, 503 -> exit 1.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python3", "-c", "import urllib.request,sys\ntry:\n    urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)\n    sys.exit(0)\nexcept Exception:\n    sys.exit(1)"]

CMD ["python3", "/app/app.py"]
