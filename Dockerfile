# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb'))['project']; \
open('/tmp/req.txt','w').write('\n'.join(d['dependencies'])); \
open('/tmp/req-dev.txt','w').write('\n'.join(d['optional-dependencies']['dev']))" \
 && pip install -r /tmp/req.txt

# Tests and tooling: the source is bind-mounted at /app.
FROM base AS dev
RUN pip install -r /tmp/req-dev.txt
ENV PYTHONPATH=/app/src

FROM base AS runtime
COPY src ./src
RUN pip install --no-deps . && rm -rf /app/src /app/build
COPY docker/entrypoint.sh /entrypoint.sh
ENV MW_DATA_DIR=/data \
    MW_MEDIA_DIR=/media \
    MW_PORT=8790
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('MW_PORT','8790'), timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["mediawrangler"]
