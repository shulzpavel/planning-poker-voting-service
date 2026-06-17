FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=60

# Avoid tz/locale prompts during install
ENV DEBIAN_FRONTEND=noninteractive

# Needed for container healthcheck (curl)
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY backend/services/voting_service/requirements.txt .
RUN PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    pip install --no-cache-dir --default-timeout=${PIP_DEFAULT_TIMEOUT} -i ${PIP_INDEX_URL} -r requirements.txt

# Copy service code
COPY backend/services/ ./services/
COPY backend/app/ ./app/
COPY backend/config.py .
COPY backend/session_store.py .

# Expose port
EXPOSE 8002

# Run service
CMD ["python", "-m", "services.voting_service.main"]
