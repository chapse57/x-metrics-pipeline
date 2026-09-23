# One image, two jobs (see docker-compose.yml):
#   setup  — migrate the schema, load the SQLite files, create the read-only login   (owner DSN)
#   api    — serve the read API                                                       (reader DSN only)
# Only the three pure-Python packages the pg/ and api/ layers need are installed. The collector
# (Playwright, a browser, the LLM client) is not in this image: nothing here writes to X or to
# the database except `setup`, and `setup` only loads files it is given.
FROM python:3.12-slim

WORKDIR /app
COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY xmetrics/ /app/xmetrics/
COPY pg/ /app/pg/
COPY api/ /app/api/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
