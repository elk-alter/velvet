FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py library_core.py index.html VERSION ./
ENV PYTHONUNBUFFERED=1 VELVET_SERVER=1 VELVET_DATA=/data
EXPOSE 8765
CMD ["python", "app.py", "--no-browser"]
