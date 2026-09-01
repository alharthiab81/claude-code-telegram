FROM python:3.12-slim

WORKDIR /app

# Build tools for any dependency that needs to compile
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
        && rm -rf /var/lib/apt/lists/*

        # Install poetry and configure it to install straight into the system
        # environment (no extra virtualenv layer needed inside a container)
        RUN pip install --no-cache-dir poetry==2.4.1 \
            && poetry config virtualenvs.create false

            # Install dependencies first so Docker can cache this layer between builds
            COPY pyproject.toml poetry.lock ./
            RUN poetry install --no-root --only main --no-interaction --no-ansi

            # Now copy the actual application source
            COPY . .

            # Make sure the mount points the bot expects exist even on a fresh volume
            RUN mkdir -p /app/projects /app/data

            CMD ["python", "-m", "src.main"]
