# Kuro 部署指南

本文檔提供 Kuro 在不同作業系統和環境下的完整部署說明。

---

## 目錄

- [硬體需求](#硬體需求)
- [macOS 部署](#macos-部署)
- [Windows 部署](#windows-部署)
- [Linux 部署](#linux-部署)
- [Docker 部署](#docker-部署)
- [雲端伺服器部署](#雲端伺服器部署)
- [常見問題](#常見問題)

---

## 硬體需求

### 僅使用雲端模型（Claude/GPT/Gemini）

| 組件 | 最低需求 | 推薦配置 |
|------|---------|---------|
| **CPU** | 雙核心 2GHz | 四核心 3GHz+ |
| **RAM** | 2GB | 4GB |
| **硬碟** | 2GB | 10GB |
| **網路** | 穩定連線 | 10Mbps+ |

### 運行本地模型（Ollama）

| 模型大小 | RAM 需求 | 推薦 CPU | 推薦 GPU |
|---------|---------|---------|---------|
| **7B** (llama3.1, qwen2.5:7b) | 8GB | 4核心+ | 可選 |
| **13B** | 16GB | 6核心+ | 4GB+ VRAM |
| **32B** (qwen2.5:32b) | 32GB | 8核心+ | 8GB+ VRAM |
| **70B** (llama3.1:70b) | 64GB | 12核心+ | 24GB+ VRAM |

---

## macOS 部署

### 支援版本

- macOS 11 (Big Sur) 或更新版本
- **Apple Silicon (M1/M2/M3/M4) - 強烈推薦** ⭐
- Intel Mac - 支援但建議使用雲端模型

### 安裝步驟

#### 1. 安裝 Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

#### 2. 安裝 Python 3.12+

```bash
# 安裝 Python
brew install python@3.12

# 驗證安裝
python3.12 --version
```

#### 3. 安裝 Poetry

```bash
# 安裝 Poetry
curl -sSL https://install.python-poetry.org | python3 -

# 設定 PATH（zsh）
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 或 bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile
source ~/.bash_profile

# 驗證
poetry --version
```

#### 4. 安裝 Ollama（可選，用於本地模型）

```bash
# 方法 1: Homebrew（推薦）
brew install ollama

# 啟動 Ollama 服務
brew services start ollama

# 方法 2: 官方安裝包
# 下載：https://ollama.ai/download/mac
```

#### 5. 下載本地模型

```bash
# 推薦：7B 模型（Apple Silicon 流暢運行）
ollama pull llama3.1

# 中文優化模型
ollama pull qwen2.5:7b

# 程式碼專用
ollama pull deepseek-coder-v2:16b

# 如果你有 32GB+ RAM (M2 Pro/Max)
ollama pull qwen2.5:32b

# 驗證
ollama list
```

#### 6. Clone 並安裝 Kuro

```bash
# Clone 專案
cd ~/Projects  # 或你想放的位置
git clone <your-repo-url> kuro
cd kuro

# 安裝依賴
poetry install

# （可選）安裝 Playwright 瀏覽器支援
poetry run playwright install chromium

# 初始化設定
poetry run kuro --init
```

#### 7. 設定環境變數

```bash
# 複製環境變數範本
cp .env.example .env

# 編輯 .env 加入 API keys
nano .env
```

`.env` 內容：
```bash
# 雲端模型 API Keys（可選）
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...

# Telegram/Discord（可選）
KURO_TELEGRAM_TOKEN=...
KURO_DISCORD_TOKEN=...
```

#### 8. 設定 config.yaml（macOS 最佳化）

編輯 `~/.kuro/config.yaml`：

```yaml
models:
  # Apple Silicon 優先使用本地模型
  default: "ollama/llama3.1"

  fallback_chain:
    - "ollama/llama3.1"
    - "anthropic/claude-sonnet-4-20250514"

  providers:
    ollama:
      base_url: "http://localhost:11434"
      api_key: "not-needed"
    anthropic:
      api_key_env: "ANTHROPIC_API_KEY"

sandbox:
  allowed_directories:
    - "~/Documents"
    - "~/Desktop"
    - "~/Downloads"
    - "~/Projects"

agents:
  enabled: true
  predefined:
    - name: fast
      model: ollama/llama3.1
      max_tool_rounds: 3
```

#### 9. 啟動 Kuro

```bash
# CLI 模式
poetry run kuro

# Web GUI 模式
poetry run kuro --web
# 打開 http://127.0.0.1:7860
```

### macOS 特有最佳化

#### 建立桌面快速啟動

```bash
# 建立 command 檔
cat > ~/Desktop/Kuro.command << 'EOF'
#!/bin/bash
cd ~/Projects/kuro
export PATH="$HOME/.local/bin:$PATH"
poetry run kuro --web
EOF

# 賦予執行權限
chmod +x ~/Desktop/Kuro.command
```

雙擊 `Kuro.command` 即可啟動！

#### 建立 launchd 自動啟動服務

建立 `~/Library/LaunchAgents/com.kuro.assistant.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kuro.assistant</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOUR_USERNAME/.local/bin/poetry</string>
        <string>run</string>
        <string>kuro</string>
        <string>--web</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/YOUR_USERNAME/Projects/kuro</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/kuro.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/kuro.err</string>
</dict>
</plist>
```

**啟用服務**：
```bash
# 載入服務
launchctl load ~/Library/LaunchAgents/com.kuro.assistant.plist

# 立即啟動
launchctl start com.kuro.assistant

# 停止服務
launchctl stop com.kuro.assistant
```

---

## Windows 部署

### 支援版本

- Windows 10 (1909+) 或 Windows 11
- Windows Server 2019+

### 安裝步驟

#### 1. 安裝 Python 3.12+

**方法 1: Microsoft Store（推薦）**
```
1. 打開 Microsoft Store
2. 搜尋 "Python 3.12"
3. 點擊安裝
```

**方法 2: python.org**
```
1. 下載：https://www.python.org/downloads/
2. 執行安裝程式
3. ✅ 勾選 "Add Python to PATH"
4. 點擊 "Install Now"
```

驗證安裝：
```powershell
python --version
# 應顯示 Python 3.12.x
```

#### 2. 安裝 Poetry

```powershell
# PowerShell（管理員權限）
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | python -

# 設定 PATH
# 將 %APPDATA%\Python\Scripts 加入系統環境變數 PATH

# 驗證
poetry --version
```

#### 3. 安裝 Git（如果還沒有）

```
下載：https://git-scm.com/download/win
安裝時使用預設選項
```

#### 4. 安裝 Ollama（可選）

```
下載：https://ollama.ai/download/windows
執行安裝程式
Ollama 會自動在背景執行
```

下載模型：
```powershell
# PowerShell
ollama pull llama3.1
ollama pull qwen2.5:7b
ollama list
```

#### 5. Clone 並安裝 Kuro

```powershell
# PowerShell
cd C:\Users\YourName\Projects  # 或你想放的位置
git clone <your-repo-url> kuro
cd kuro

# 安裝依賴
poetry install

# （可選）安裝 Playwright
poetry run playwright install chromium

# 初始化設定
poetry run kuro --init
```

#### 6. 設定環境變數

```powershell
# 複製 .env 範本
copy .env.example .env

# 用記事本編輯
notepad .env
```

#### 7. 設定 config.yaml（Windows 路徑格式）

編輯 `C:\Users\YourName\.kuro\config.yaml`：

```yaml
models:
  default: "ollama/llama3.1"

  providers:
    ollama:
      base_url: "http://localhost:11434"
      api_key: "not-needed"

sandbox:
  allowed_directories:
    - "C:\\Users\\YourName\\Documents"
    - "C:\\Users\\YourName\\Desktop"
    - "C:\\Users\\YourName\\Downloads"

  blocked_commands:
    - "format"
    - "del /f /s /q C:\\"
    - "rd /s /q C:\\"
```

⚠️ **注意**：Windows 路徑需使用雙反斜線 `\\` 或單斜線 `/`

#### 8. 啟動 Kuro

```powershell
# CLI 模式
poetry run kuro

# Web GUI 模式
poetry run kuro --web
# 打開 http://127.0.0.1:7860
```

### Windows 特有最佳化

#### 建立桌面捷徑

建立 `Kuro.bat`：
```batch
@echo off
cd /d C:\Users\YourName\Projects\kuro
poetry run kuro --web
pause
```

右鍵 > 傳送到 > 桌面（建立捷徑）

#### 建立 Windows 服務（開機自動啟動）

使用 NSSM（Non-Sucking Service Manager）：

```powershell
# 下載 NSSM：https://nssm.cc/download
# 解壓後執行

nssm install Kuro "C:\Users\YourName\AppData\Roaming\Python\Scripts\poetry.exe" "run kuro --web"
nssm set Kuro AppDirectory "C:\Users\YourName\Projects\kuro"
nssm start Kuro
```

---

## Linux 部署

### 支援發行版

- Ubuntu 20.04+ / Debian 11+
- CentOS 8+ / RHEL 8+
- Fedora 35+
- Arch Linux

### Ubuntu/Debian 安裝步驟

#### 1. 更新系統並安裝依賴

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git curl
```

如果 Python 3.12 不在倉庫：
```bash
# 加入 deadsnakes PPA
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

#### 2. 安裝 Poetry

```bash
curl -sSL https://install.python-poetry.org | python3.12 -

# 設定 PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

poetry --version
```

#### 3. 安裝 Ollama（可選）

```bash
curl -fsSL https://ollama.ai/install.sh | sh

# 驗證服務運行
systemctl status ollama

# 下載模型
ollama pull llama3.1
```

#### 4. Clone 並安裝 Kuro

```bash
cd ~/projects
git clone <your-repo-url> kuro
cd kuro

poetry install

# 初始化
poetry run kuro --init
```

#### 5. 設定環境變數

```bash
cp .env.example .env
nano .env  # 編輯 API keys
```

#### 6. （可選）安裝 Playwright（需要額外依賴）

```bash
# 安裝系統依賴
sudo apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

# 安裝 Playwright
poetry run playwright install chromium
```

#### 7. 啟動 Kuro

```bash
# CLI 模式
poetry run kuro

# Web GUI 模式（背景執行）
nohup poetry run kuro --web > /tmp/kuro.log 2>&1 &

# 查看日誌
tail -f /tmp/kuro.log
```

### 建立 systemd 服務（自動啟動）

建立 `/etc/systemd/system/kuro.service`：

```ini
[Unit]
Description=Kuro AI Assistant
After=network.target ollama.service

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/projects/kuro
Environment="PATH=/home/YOUR_USERNAME/.local/bin:/usr/bin"
ExecStart=/home/YOUR_USERNAME/.local/bin/poetry run kuro --web
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**啟用服務**：
```bash
sudo systemctl daemon-reload
sudo systemctl enable kuro
sudo systemctl start kuro

# 查看狀態
sudo systemctl status kuro

# 查看日誌
sudo journalctl -u kuro -f
```

### Nginx 反向代理（可選）

```nginx
# /etc/nginx/sites-available/kuro
server {
    listen 80;
    server_name kuro.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;

        # WebSocket 支援
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
# 啟用站點
sudo ln -s /etc/nginx/sites-available/kuro /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## Docker 部署

### 方法 1: Docker Compose（推薦）

建立 `docker-compose.yml`：

```yaml
version: '3.8'

services:
  ollama:
    image: ollama/ollama:latest
    container_name: kuro-ollama
    volumes:
      - ollama-data:/root/.ollama
    ports:
      - "11434:11434"
    restart: unless-stopped
    # GPU 支援（NVIDIA）
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: 1
    #           capabilities: [gpu]

  kuro:
    build: .
    container_name: kuro-assistant
    depends_on:
      - ollama
    volumes:
      - kuro-data:/root/.kuro
      - ./skills:/root/.kuro/skills
      - ./plugins:/root/.kuro/plugins
    ports:
      - "7860:7860"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
    restart: unless-stopped

volumes:
  ollama-data:
  kuro-data:
```

建立 `Dockerfile`：

```dockerfile
FROM python:3.12-slim

# 安裝系統依賴
RUN apt-get update && apt-get install -y \
    curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Poetry
RUN curl -sSL https://install.python-poetry.org | python3 - \
    && ln -s /root/.local/bin/poetry /usr/local/bin/poetry

# 設定工作目錄
WORKDIR /app

# 複製專案檔案
COPY pyproject.toml poetry.lock ./
COPY src ./src
COPY README.md ./

# 安裝依賴
RUN poetry config virtualenvs.create false \
    && poetry install --no-dev --no-interaction --no-ansi

# 暴露埠
EXPOSE 7860

# 啟動指令
CMD ["poetry", "run", "kuro", "--web"]
```

**啟動服務**：
```bash
# 建立 .env 檔（與 docker-compose.yml 同目錄）
cat > .env << EOF
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
EOF

# 啟動所有服務
docker-compose up -d

# 查看日誌
docker-compose logs -f kuro

# 進入 Ollama 容器下載模型
docker exec -it kuro-ollama ollama pull llama3.1

# 停止服務
docker-compose down
```

### 方法 2: 單一 Docker 容器

```bash
# 建立映像
docker build -t kuro:latest .

# 執行容器
docker run -d \
  --name kuro \
  -p 7860:7860 \
  -v ~/.kuro:/root/.kuro \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  kuro:latest

# 查看日誌
docker logs -f kuro
```

---

## 雲端伺服器部署

### AWS EC2

**推薦實例類型**：
- **僅雲端模型**：t3.small (2 vCPU, 2GB RAM)
- **7B 本地模型**：t3.large (2 vCPU, 8GB RAM)
- **32B 本地模型 + GPU**：g4dn.xlarge (4 vCPU, 16GB RAM, T4 GPU)

**安全群組設定**：
```
入站規則：
- 7860 (Web GUI) - 限制你的 IP
- 22 (SSH) - 限制你的 IP
```

**部署步驟**：
```bash
# SSH 連線
ssh -i your-key.pem ubuntu@your-ec2-ip

# 按照 Linux 部署步驟安裝
# 設定 systemd 服務
# 設定 Nginx（可選 HTTPS）
```

### Google Cloud Platform (GCP)

**推薦機器類型**：
- **僅雲端**：e2-small (2 vCPU, 2GB)
- **7B 本地**：e2-standard-2 (2 vCPU, 8GB)
- **GPU**：n1-standard-4 + NVIDIA T4

**防火牆規則**：
```bash
gcloud compute firewall-rules create allow-kuro \
  --allow tcp:7860 \
  --source-ranges YOUR_IP/32
```

### Azure VM

**推薦大小**：
- **僅雲端**：Standard_B2s (2 vCPU, 4GB)
- **7B 本地**：Standard_D2s_v3 (2 vCPU, 8GB)

### 雲端部署最佳實踐

```yaml
# config.yaml（雲端伺服器最佳化）
models:
  # 優先雲端 API（伺服器成本 > API 成本）
  default: "anthropic/claude-haiku-4-20250414"

  # 本地作為備用（API 失敗時）
  fallback_chain:
    - "anthropic/claude-haiku-4-20250414"
    - "ollama/llama3.1"

security:
  # 嚴格安全設定
  auto_approve_levels: []  # 全部要求核准
  require_approval_for: ["*"]

sandbox:
  allowed_directories:
    - "/home/ubuntu/workspace"  # 限制範圍
  max_execution_time: 15  # 降低超時時間

web_ui:
  host: "0.0.0.0"  # 允許外部連線
  port: 7860

# 禁用本地工具（伺服器環境不需要）
security:
  disabled_tools:
    - screenshot
    - clipboard_read
    - clipboard_write
```

**HTTPS 設定（Let's Encrypt）**：
```bash
# 安裝 Certbot
sudo apt install certbot python3-certbot-nginx

# 取得憑證
sudo certbot --nginx -d kuro.yourdomain.com

# 自動續約
sudo certbot renew --dry-run
```

---

## 常見問題

### macOS

**Q: Ollama 顯示 "connection refused"？**
```bash
# 確認服務運行
brew services list | grep ollama

# 啟動服務
brew services start ollama

# 或手動啟動
ollama serve
```

**Q: Poetry 找不到？**
```bash
# 檢查 PATH
echo $PATH | grep ".local/bin"

# 加入 PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Windows

**Q: "poetry 不是內部或外部命令"？**
```powershell
# 將以下路徑加入系統 PATH：
%APPDATA%\Python\Scripts

# 或重新安裝 Poetry 時使用管理員權限
```

**Q: Ollama 沒有自動啟動？**
```powershell
# 檢查服務
Get-Service Ollama

# 手動啟動
Start-Service Ollama

# 設定自動啟動
Set-Service -Name Ollama -StartupType Automatic
```

### Linux

**Q: 權限錯誤？**
```bash
# 確保使用正確的使用者
whoami

# 不要用 sudo 執行 poetry
poetry run kuro  # ✓ 正確
sudo poetry run kuro  # ✗ 錯誤
```

**Q: Playwright 無法啟動瀏覽器？**
```bash
# 安裝缺少的依賴
sudo apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0

# 無頭環境可以禁用瀏覽器工具
# 在 config.yaml:
security:
  disabled_tools:
    - web_navigate
    - web_click
    - web_type
```

### Docker

**Q: 容器無法連線到 Ollama？**
```yaml
# docker-compose.yml 確保服務連結
services:
  kuro:
    depends_on:
      - ollama
    environment:
      - OLLAMA_BASE_URL=http://ollama:11434  # 使用服務名稱
```

**Q: GPU 不工作？**
```bash
# 確認 NVIDIA Docker runtime
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# docker-compose.yml 啟用 GPU
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

---

## 效能基準測試

### 本地模型推理速度（7B llama3.1）

| 硬體配置 | Token/秒 | 簡單回應時間 |
|---------|---------|------------|
| MacBook M1 16GB | 25-30 | 2-3 秒 |
| MacBook M2 16GB | 35-40 | 1.5-2.5 秒 |
| Ubuntu 8核/16GB (無GPU) | 8-12 | 8-12 秒 |
| Ubuntu + RTX 3060 12GB | 50-60 | 1-2 秒 |
| Ubuntu + RTX 4090 24GB | 100-120 | <1 秒 |

### 雲端 API 延遲

| 模型 | 平均延遲 | 成本/1M tokens |
|------|---------|---------------|
| claude-haiku-4 | 1-2 秒 | $0.25 |
| claude-sonnet-4 | 2-4 秒 | $3.00 |
| gpt-4o-mini | 1-2 秒 | $0.15 |
| gpt-4o | 2-3 秒 | $2.50 |

---

## 下一步

- 閱讀 [README.md](../README.md) 了解功能詳情
- 查看 [SYSTEM_PROMPT_ENCRYPTION.md](SYSTEM_PROMPT_ENCRYPTION.md) 設定系統提示加密
- 探索 Skills 和 Plugins 系統擴展功能

需要協助？提交 Issue 到專案 GitHub 倉庫。
