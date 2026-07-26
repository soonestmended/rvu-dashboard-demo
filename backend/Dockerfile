FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code as a package
COPY backend/ /app/backend/

# Set PYTHONPATH so both the backend package and the mounted scheduler module are importable
ENV PYTHONPATH=/app:/app/scheduler

# Run the server using the module
EXPOSE 5001
CMD ["python", "-m", "backend.app"]
