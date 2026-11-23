FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The garrison config lives at the repo root, outside this build context:
# mount it and point ANVIL_CONFIG_PATH at it, e.g.
#   docker run -v "$PWD/anvil-fooddash.yaml:/app/anvil-fooddash.yaml" \
#     -e ANVIL_CONFIG_PATH=/app/anvil-fooddash.yaml -e OPENAI_API_KEY \
#     -e FOODDASH_BASE_URL=http://host.docker.internal:8000 -p 8100:8100 fooddash-agent
# Callers need an ak_ key FoodDash issued: mount the hashed key file
# (python -m app.a2a issue fooddash|chathub ...) and set FOODDASH_A2A_KEYS to it.
EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
