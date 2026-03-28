FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Ensure data directory exists for SQLite
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/listbridge.db

EXPOSE 5000

CMD ["python", "app.py"]
