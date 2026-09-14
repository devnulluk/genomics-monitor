FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY genomics_monitor ./genomics_monitor
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 monitor && mkdir -p /data && chown monitor:monitor /data
USER monitor
ENV GENOMICS_DB=/data/genomics.sqlite
EXPOSE 8080
CMD ["uvicorn", "genomics_monitor.app:app", "--host", "0.0.0.0", "--port", "8080"]
