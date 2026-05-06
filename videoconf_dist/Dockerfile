FROM python:3.12.3-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV OMP_NUM_THREADS=1

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libzmq3-dev \
    libportaudio2 \
    portaudio19-dev \
    libasound2 \
    libasound2-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .