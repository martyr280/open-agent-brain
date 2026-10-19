FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 brain
WORKDIR /app
COPY pyproject.toml ./
COPY brainkit ./brainkit
RUN pip install .
USER brain
ENV BRAIN_STORE=github PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health',timeout=4)"
CMD ["sh", "-c", "brain serve --host 0.0.0.0 --port ${PORT}"]
