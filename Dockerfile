FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir "apprise>=1.9,<2" "fastapi>=0.116,<1" "python-multipart>=0.0.20,<1" "uvicorn[standard]>=0.35,<1"
COPY genomics_monitor ./genomics_monitor
RUN pip install --no-cache-dir --no-deps .
RUN useradd --system --uid 10001 monitor && mkdir -p /data && chown monitor:monitor /data
USER monitor
ENV GENOMICS_DB=/data/genomics.sqlite
EXPOSE 8080
CMD ["uvicorn", "genomics_monitor.app:app", "--host", "0.0.0.0", "--port", "8080"]
