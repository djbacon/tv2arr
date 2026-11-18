FROM python:3.14-slim
ARG TV2ARR_VERSION
LABEL org.opencontainers.image.title="tv2arr" \
      org.opencontainers.image.version="${TV2ARR_VERSION}"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt VERSION ./
RUN test "$(cat VERSION)" = "${TV2ARR_VERSION}" \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --no-create-home tv2arr \
    && install -d -m 0700 -o tv2arr -g tv2arr /data
COPY tv2arr ./tv2arr

USER 10001
EXPOSE 8080
CMD ["gunicorn", "--no-control-socket", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "tv2arr.wsgi:app"]
