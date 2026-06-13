FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Config comes from env vars (MAX2TG_*) or a mounted /app/config.json.
CMD ["python", "main.py"]
