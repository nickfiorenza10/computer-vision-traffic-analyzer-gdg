# Worker image: the pipeline, the Caltrans recorder and the CSV importer in one CPU-only
# container. Built from the repo root so it can copy the project packages:
#
#   docker build -f worker/Dockerfile -t traffic-worker .
#   docker run --rm -p 7860:7860 -e DATABASE_URL=... -e WORKER_TOKEN=... traffic-worker
#
# The build context must be the repo root (the trailing dot), because the image copies
# pipeline/, ingestion/, database/ and worker/; `docker build worker/` puts the context
# inside worker/ and cannot reach them. The image listens on 7860, the port Hugging Face
# Spaces used to expect, although the worker now runs on Michael's PC
# (worker/run_local.ps1) and the image is only for a future hosted deployment.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    OPENCV_LOG_LEVEL=ERROR

# OpenCV's runtime libraries; the wheels are not headless builds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall PyTorch.
COPY requirements.txt /app/requirements-pipeline.txt
COPY database/requirements.txt /app/requirements-database.txt
COPY worker/requirements.txt /app/requirements-worker.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install torch==2.14.1 torchvision==0.29.1 \
        --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install -r /app/requirements-pipeline.txt \
        -r /app/requirements-database.txt \
        -r /app/requirements-worker.txt

# The code the worker runs. database/ is here for load_events.py, ingestion/ for the
# Caltrans recorder, pipeline/ for track_count.py and the committed lanes files.
COPY pipeline/ /app/pipeline/
COPY ingestion/ /app/ingestion/
COPY database/ /app/database/
COPY worker/ /app/worker/

RUN mkdir -p /data && useradd --create-home --uid 1000 worker && chown -R worker:worker /data /app
USER worker

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "worker.app:app", "--host", "0.0.0.0", "--port", "7860"]
