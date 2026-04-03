FROM python:3.11-slim

RUN apt-get update -y && \
    apt-get install -y ffmpeg sox && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/bin:/usr/local/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 300
