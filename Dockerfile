# Kuro Docker Image

FROM python:3.12-slim

# 安裝系統依賴
RUN apt-get update && apt-get install -y \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="/root/.local/bin:$PATH"

# 建立工作目錄
WORKDIR /app

# 複製專案檔案
COPY pyproject.toml poetry.lock ./
COPY src ./src
COPY README.md ./

# 安裝 Python 依賴
RUN poetry config virtualenvs.create false \
    && poetry install --no-dev --no-interaction --no-ansi

# 建立資料目錄
RUN mkdir -p /app/data

# 設定環境變數
ENV PYTHONUNBUFFERED=1
ENV KURO_HOME=/app/data

# 預設指令
CMD ["poetry", "run", "kuro", "discord"]
