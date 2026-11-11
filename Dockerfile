# Filesystem MCP server over Streamable HTTP.
#   docker build -t resume-fs-mcp .
#   docker run -p 8765:8765 -v "$PWD/data:/app/data" resume-fs-mcp
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "pypdf>=4" "python-docx>=1.1"
COPY mcp_protocol.py filesystem_mcp_server.py ./
COPY config ./config
ENV FS_MCP_TRANSPORT=http \
    FS_MCP_HTTP_HOST=0.0.0.0 \
    FS_MCP_HTTP_PORT=8765 \
    FS_MCP_ROOT_DIR=/app/data \
    FS_MCP_LOG_LEVEL=INFO
RUN useradd --create-home mcp && mkdir -p /app/data && chown -R mcp /app
USER mcp
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["python", "filesystem_mcp_server.py"]
