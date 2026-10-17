# Playwright image includes the matching Chromium + OS libraries.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY browser.py parse_trains.py general_form.py watcher.py ./
CMD ["python", "-u", "watcher.py"]
