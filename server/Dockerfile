FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy
WORKDIR /srv
COPY server/requirements.txt /srv/server/requirements.txt
RUN pip install --no-cache-dir -r /srv/server/requirements.txt
COPY tracker /srv/tracker
COPY server /srv/server
COPY docs /srv/docs
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
