FROM python:3.12-slim
# fonts-roboto + fonts-noto-core: Android jaise asli fonts (JS se font spoof nahi hota, isliye system me hi rakhe)
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-roboto fonts-noto-core curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Obscura (halka browser). Download fail ho to build NAHI tootega, app Chromium par chalegi.
RUN set -x; \
    case "$(uname -m)" in x86_64) A=x86_64;; aarch64|arm64) A=aarch64;; *) A=none;; esac; \
    if [ "$A" != "none" ] && curl -fsSL --retry 3 -o /tmp/obscura.tgz \
         "https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-${A}-linux.tar.gz" \
       && mkdir -p /opt/obscura && tar xzf /tmp/obscura.tgz -C /opt/obscura; then \
         find /opt/obscura -type f -name obscura -exec cp {} /usr/local/bin/obscura \; ; \
         find /opt/obscura -type f -name obscura-worker -exec cp {} /usr/local/bin/obscura-worker \; ; \
         chmod +x /usr/local/bin/obscura /usr/local/bin/obscura-worker 2>/dev/null || true; \
         /usr/local/bin/obscura --version || true; \
       else echo "obscura install nahi hua, Chromium par chalega"; fi; \
    rm -f /tmp/obscura.tgz

RUN pip install --no-cache-dir playwright tzdata
ENV CHROME_PATH=/usr/bin/chromium
WORKDIR /app
COPY app.py .
CMD ["python", "-u", "app.py"]
