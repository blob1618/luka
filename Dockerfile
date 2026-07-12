FROM python:3.11-slim

WORKDIR /home/app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Use a shell form so environment variables (like $PORT) are expanded at runtime.
# The scheduler must run once, not once per Gunicorn worker.
CMD ["sh", "-c", "gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT:-8000}"]
