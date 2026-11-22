#!/usr/bin/env python3
"""Minimal internal HTTP service converting PCM audio or Piper TTS into Discord OGG Opus voice messages."""

import array
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISCORD_UPLOAD_PREFIX = "https://discord-attachments-uploads-prd.storage.googleapis.com/"

MODEL_PATH = os.environ.get("PIPER_MODEL_PATH", "/models/fr_FR-siwis-medium.onnx")
MODEL_CFG_PATH = f"{MODEL_PATH}.json"


def load_model_sample_rate() -> int:
    try:
        with open(MODEL_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return int(cfg.get("audio", {}).get("sample_rate", 22050))
    except Exception:
        return 22050


MODEL_SAMPLE_RATE = load_model_sample_rate()


def build_discord_waveform(pcm_bytes: bytes) -> str:
    """Compute a 256-sample base64 waveform from 16-bit signed LE mono PCM."""
    if len(pcm_bytes) % 2:
        pcm_bytes = pcm_bytes[:-1]
    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        samples.byteswap()
    n = len(samples)
    if n == 0:
        return base64.b64encode(bytes(256)).decode("ascii")

    rms_bins = []
    for i in range(256):
        start = (i * n) // 256
        end = ((i + 1) * n) // 256
        if end <= start:
            end = min(n, start + 1)
        seg = samples[start:end]
        mean_sq = sum(s * s for s in seg) / len(seg)
        rms_bins.append(math.sqrt(mean_sq))

    peak = max(max(rms_bins), 1000.0)
    scaled = bytes(max(0, min(255, int(round((val / peak) * 255)))) for val in rms_bins)
    return base64.b64encode(scaled).decode("ascii")


def pcm_to_discord_opus(pcm_bytes: bytes, sample_rate: int) -> dict:
    """Convert raw s16le mono PCM to OGG Opus + Discord duration/waveform metadata."""
    if len(pcm_bytes) % 2:
        pcm_bytes = pcm_bytes[:-1]
    if not pcm_bytes:
        raise ValueError("Empty PCM audio input")

    duration_secs = round((len(pcm_bytes) / 2.0) / float(sample_rate), 2)
    waveform = build_discord_waveform(pcm_bytes)

    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-application",
            "voip",
            "-f",
            "ogg",
            "pipe:1",
        ],
        input=pcm_bytes,
        capture_output=True,
        check=True,
    )
    ogg_bytes = proc.stdout
    if not ogg_bytes.startswith(b"OggS"):
        raise RuntimeError("ffmpeg did not produce a valid OGG stream")

    return {
        "ogg_base64": base64.b64encode(ogg_bytes).decode("ascii"),
        "duration_secs": duration_secs,
        "waveform": waveform,
        "size_bytes": len(ogg_bytes),
    }


def synthesize_piper_pcm(text: str) -> bytes:
    """Run Piper TTS in a fresh subprocess so ONNX runtime memory is released immediately."""
    piper_bin = shutil.which("piper")
    cmd = [piper_bin] if piper_bin else [sys.executable, "-m", "piper"]
    cmd.extend(["--model", MODEL_PATH, "--output-raw"])

    proc = subprocess.run(
        cmd,
        input=text.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"piper failed ({proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:400]}")
    return proc.stdout


class TTSHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[homelab-tts] {fmt % args}\n")

    def send_json(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "model": os.path.basename(MODEL_PATH), "sample_rate": MODEL_SAMPLE_RATE})
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        t0 = time.monotonic()
        try:
            data = self.read_json()
            if self.path == "/convert/pcm-to-opus":
                pcm_b64 = data.get("pcm_base64", "")
                sr = int(data.get("sample_rate", 24000))
                if not pcm_b64:
                    self.send_json(400, {"error": "Missing pcm_base64"})
                    return
                pcm_bytes = base64.b64decode(pcm_b64)
                res = pcm_to_discord_opus(pcm_bytes, sr)
                res["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                self.send_json(200, res)
                return

            if self.path == "/tts/piper":
                text = str(data.get("text", "")).strip()
                if not text:
                    self.send_json(400, {"error": "Missing text"})
                    return
                pcm_bytes = synthesize_piper_pcm(text)
                res = pcm_to_discord_opus(pcm_bytes, MODEL_SAMPLE_RATE)
                res["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                self.send_json(200, res)
                return

            if self.path == "/upload-ogg":
                upload_url = str(data.get("upload_url", "")).strip()
                ogg_b64 = data.get("ogg_base64", "")
                if not upload_url.startswith(DISCORD_UPLOAD_PREFIX):
                    self.send_json(400, {"error": "Invalid Discord upload_url prefix"})
                    return
                if not ogg_b64:
                    self.send_json(400, {"error": "Missing ogg_base64"})
                    return
                ogg_bytes = base64.b64decode(ogg_b64)
                if not ogg_bytes.startswith(b"OggS"):
                    self.send_json(400, {"error": "Invalid OggS binary payload"})
                    return
                req = urllib.request.Request(
                    upload_url,
                    data=ogg_bytes,
                    headers={
                        "Content-Type": "audio/ogg",
                        "Content-Length": str(len(ogg_bytes)),
                    },
                    method="PUT",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.status
                self.send_json(
                    200,
                    {
                        "status": status,
                        "uploaded_bytes": len(ogg_bytes),
                        "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    },
                )
                return

            self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), TTSHandler)
    print(f"homelab-tts listening on :8000 (model={MODEL_PATH}, sr={MODEL_SAMPLE_RATE})", flush=True)
    server.serve_forever()
