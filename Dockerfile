# qoder2api — Python Flask server
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py cosy.py qoder_oauth.py ./

ENV PORT=8080 HOST=0.0.0.0
EXPOSE 8080

# QODER_CREDENTIAL (and optionally API_KEY) must be provided at runtime:
#   docker run -p 8080:8080 -e QODER_CREDENTIAL='...' -e API_KEY='...' qoder2api
CMD ["sh", "-c", "exec python server.py"]
