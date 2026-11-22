FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir piper-tts

RUN mkdir -p /models /app \
 && curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx" \
      -o /models/fr_FR-siwis-medium.onnx \
 && curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx.json" \
      -o /models/fr_FR-siwis-medium.onnx.json

COPY server.py /app/server.py

EXPOSE 8000
CMD ["python3", "/app/server.py"]
