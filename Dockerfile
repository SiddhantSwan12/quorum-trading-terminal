FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY server/requirements-cloud.txt /app/server/requirements-cloud.txt
RUN pip install -r server/requirements-cloud.txt
COPY TradingAgents /app/TradingAgents
RUN pip install /app/TradingAgents
COPY server /app/server
ENV QUORUM_DATA_DIR=/tmp/quorum \
    TRADINGAGENTS_CACHE_DIR=/tmp/agents/cache \
    TRADINGAGENTS_RESULTS_DIR=/tmp/agents/results \
    TRADINGAGENTS_MEMORY_LOG_PATH=/tmp/agents/memory/trading_memory.md
EXPOSE 8000
CMD ["sh", "-c", "exec python -m uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
