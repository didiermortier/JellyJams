FROM python:3.11-alpine

# Set working directory and copy app files
WORKDIR /app
COPY app /app
RUN chmod +x /app/*.sh

# Install required packages
RUN apk add --no-cache su-exec
RUN pip install --no-cache-dir -r /app/requirements.txt

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV JELLYFIN_URL=http://jellyfin:8096
ENV LOG_LEVEL=DEBUG
ENV ENABLE_WEB_UI=true
ENV WEB_PORT=5000

# Expose web UI port
EXPOSE 5000

# A failed generation makes the container report unhealthy, so any monitor notices.
# A fresh install with no run recorded yet is treated as healthy.
HEALTHCHECK --interval=5m --timeout=15s --start-period=60s --retries=3 \
  CMD python3 -c "import json,sys;from pathlib import Path;p=Path('/data/run_status.json');s=json.loads(p.read_text()) if p.exists() else {};sys.exit(0 if s.get('success',True) else 1)"

# Set the entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]