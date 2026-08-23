FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY quality.py ./
COPY dq ./dq
COPY contracts ./contracts
RUN pip install --no-cache-dir .

ENTRYPOINT ["data-quality"]
CMD ["--help"]
