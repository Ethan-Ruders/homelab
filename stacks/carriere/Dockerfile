FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    "uvicorn[standard]==0.32.1" \
    psycopg2-binary==2.9.10 \
    httpx==0.28.1 \
    openpyxl==3.1.5 \
    jinja2==3.1.5 \
    python-multipart==0.0.20

COPY app/ /app/app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
