FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-vision.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-vision.txt
COPY app.py resource_leases.py media_locations.py organization_jobs.py organization_service.py media_companions.py companion_bindings.py organization_executor.py organization_moves.py organization_cleanup.py data_guard.py library_transport.py runtime_config.py directory_policy.py task_journal.py storage_inventory.py inventory_query.py service_control.py full_backup.py restore_safety.py media_naming.py library_core.py video_ids.py library_query.py builtin_scraper.py media_libraries.py network_checks.py source_catalog.py people_catalog.py artwork_store.py file_review.py visual_analysis.py sources.json management.js disk_manager.js management.css manager_routes.js browse.js server_browse.js browse.css film_detail.js file_review.js index.html VERSION LICENSE NOTICE ./
COPY scrapers ./scrapers
ENV PYTHONUNBUFFERED=1 VELVET_SERVER=1 VELVET_DATA=/data
EXPOSE 8765
CMD ["python", "app.py", "--no-browser"]
