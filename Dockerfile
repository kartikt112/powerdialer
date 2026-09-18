FROM python:3.12-slim

WORKDIR /app

# listprep.py runs server-side on uploaded Excel/CSV lead lists
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY listprep.py config.yaml ./
COPY dialer/ dialer/

# Lead lists, call history, and DNC live in SQLite on the volume (DATA_DIR)
# — the image ships no data. Railway injects PORT; DATA_DIR /
# DIALER_PASSWORD / TWILIO_* come from service variables.
ENV HOST=0.0.0.0 PYTHONUNBUFFERED=1

CMD ["python", "dialer/serve.py", "--no-open"]
