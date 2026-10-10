FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# curl is handy for container healthchecks/debugging;
# ffmpeg stitches print timelapses from camera frames.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./backend

EXPOSE 8080

# The frontend/ directory is mounted or copied alongside backend/ at deploy
# time; the app serves it when present and runs API-only otherwise.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8080"]
