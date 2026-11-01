FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py library_core.py builtin_scraper.py index.html VERSION LICENSE NOTICE ./
COPY scrapers ./scrapers
ENV PYTHONUNBUFFERED=1 VELVET_SERVER=1 VELVET_DATA=/data
EXPOSE 8765
CMD ["python", "app.py", "--no-browser"]
