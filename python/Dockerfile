FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.16
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY rag ./rag
COPY fixtures ./fixtures
USER 65532:65532
ENV GRPC_ADDR=0.0.0.0:50051
EXPOSE 50051
CMD [".venv/bin/python", "-m", "app.server"]
