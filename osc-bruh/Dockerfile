FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastapi>=0.110.0" "uvicorn[standard]>=0.29.0" "pydantic>=2.0.0"

COPY x32.py x32_rest_api.py ./

EXPOSE 8080

ENTRYPOINT ["python", "x32_rest_api.py"]
