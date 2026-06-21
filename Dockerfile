FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml requirements.txt ./
COPY src/ src/
COPY config/ config/
COPY serve_dashboard.py .
COPY agentcore_agent.py .
COPY scripts/ scripts/

RUN pip install --no-cache-dir uvicorn[standard] starlette pyyaml tiktoken aiohttp boto3

EXPOSE 8000

CMD ["python", "serve_dashboard.py"]
