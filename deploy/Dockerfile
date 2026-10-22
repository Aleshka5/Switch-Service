FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY switch_service ./switch_service
RUN pip install --no-cache-dir .

# Root inside the image is correct for a rootless Podman unit: the user
# namespace already maps that root to the account that owns the socket.
# Dropping to an unmapped uid makes the mounted podman.sock unusable.
EXPOSE 8095
ENTRYPOINT ["switch-service"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8095", "--config", "/config/services.yaml"]
