# SMC backend in a Linux container (CPU build). Needed on Windows machines whose
# Application Control / Smart App Control policy blocks PyTorch's native DLLs.
#
#   docker compose up -d --build        (from the SMC folder; see docker-compose.yml)
#
# Data, models, .env and the InsightFace models are NOT baked in: they are mounted
# at run time (compose file), so the image holds code and libraries only.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential: a few pinned packages (torchreid, insightface) compile native
# helpers on Linux. libgl1/libglib2.0-0/libgomp1: runtime libraries OpenCV, PyTorch
# and onnxruntime link against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch first (the default PyPI wheel on Linux is the multi-GB CUDA build),
# then the rest of the pinned requirements; requirements.txt's torch pins are already met.
COPY requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0 \
    && pip install -r requirements.txt

# Ultralytics' tracker utilities need `lap`; without it they pip-install it at run
# time (from the internet, into the container, lost on every re-create).
RUN pip install "lap>=0.5.12"

COPY app ./app
COPY scripts ./scripts
COPY config ./config

# 0.0.0.0 inside the container; compose publishes it on 127.0.0.1 only.
ENV HOST=0.0.0.0 PORT=8822 DATA_DIR=/data MODEL_DIR=/app/models OMP_NUM_THREADS=2
EXPOSE 8822

CMD ["python", "-m", "app.serve"]
