FROM python:3.11-slim

WORKDIR /app

# Chromium for POST /capture/url. Browsers live outside the root home so the
# runtime user matches the install. Give the Cloud Run service at least 1GiB.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["python", "main.py"]
