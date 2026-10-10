FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright
ENV CHROME_PATH=/usr/bin/chromium
WORKDIR /app
COPY app.py .
CMD ["python", "-u", "app.py"]
