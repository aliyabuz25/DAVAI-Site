FROM python:3.12-slim

# Install curl (used by proxy.py for upstream requests)
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY . .

# Create data directory for SQLite databases
RUN mkdir -p /app/data

EXPOSE 5050

CMD ["python3", "proxy.py"]