# Training image. Separate from the API so PyTorch never ships in the service.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /repo

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/ml-requirements.txt

# CPU-only wheels. The default PyPI torch wheel drags in the whole CUDA stack
# (several GB) that this project has no use for.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /tmp/ml-requirements.txt

CMD ["python", "-c", "import torch, torch_geometric; print(torch.__version__, torch_geometric.__version__)"]
