FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# staff_duty/ is now a real subdir in the split repo; just copy the tree.
COPY . .

# Non-root runtime user; /data is the Fly volume mount point owned by this uid
RUN useradd --system --uid 1000 --home /app --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "150", "--graceful-timeout", "30", "staff_duty.app:app"]
