# Docker 部署指南 - 運行多個隔離的 Kuro 實例

本指南說明如何使用 Docker 運行多個完全隔離的 Kuro 實例。

---

## 使用情境

- **個人版 + Discord 公用版** - 同時運行私人助理和公用 Discord Bot
- **多個 Discord Bot** - 在不同伺服器運行不同配置的 Bot
- **開發 + 生產環境** - 隔離測試和正式環境

---

## 前置需求

### 安裝 Docker

**Windows**:
```powershell
# 下載並安裝 Docker Desktop
# https://www.docker.com/products/docker-desktop/

# 啟用 WSL 2 後端（推薦）
wsl --install
wsl --set-default-version 2
```

**Linux**:
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# 啟動 Docker
sudo systemctl start docker
sudo systemctl enable docker
```

**macOS**:
```bash
# 使用 Homebrew 安裝
brew install --cask docker

# 或下載 Docker Desktop
# https://www.docker.com/products/docker-desktop/
```

### 安裝 Docker Compose

Docker Desktop 已包含 Docker Compose。Linux 用戶需要另外安裝：

```bash
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

---

## 快速開始

### 1. 準備配置檔

```bash
cd F:/coding/assistant

# 複製環境變數範例
cp .env.example .env

# 編輯 .env，填入 API Keys
notepad .env
```

**.env 範例**:
```bash
# LLM API Keys
ANTHROPIC_API_KEY=sk-ant-your-key-here
OPENAI_API_KEY=sk-your-key-here
GEMINI_API_KEY=your-gemini-key

# Discord Bot Token
DISCORD_BOT_TOKEN=MTIzNDU2Nzg5.your-discord-token
```

### 2. 建立資料目錄

```bash
# 建立隔離的資料目錄
mkdir -p data/personal/.kuro
mkdir -p data/discord/.kuro
mkdir -p data/discord/workspace
mkdir -p data/ollama
```

### 3. 啟動服務

```bash
# 建立 Docker 映像檔
docker-compose build

# 啟動所有服務
docker-compose up -d

# 查看運行狀態
docker-compose ps

# 查看日誌
docker-compose logs -f
```

### 4. 驗證運行

```bash
# 檢查個人版（Web GUI）
# 瀏覽器開啟: http://localhost:7860

# 檢查 Discord Bot
# Discord 伺服器中測試: @Kuro 你好

# 查看容器狀態
docker ps
```

---

## 服務說明

### kuro-personal（個人版）

**用途**: 個人使用的 Kuro，提供 Web GUI 界面

**端口**: `7860` (Web GUI)

**資料位置**: `./data/personal/.kuro/`

**存取權限**:
- ✅ 讀寫 `~/Documents`
- ✅ 讀寫 `~/Desktop`
- ✅ 所有工具可用

**啟動指令**:
```bash
docker-compose up -d kuro-personal

# 查看日誌
docker-compose logs -f kuro-personal
```

### kuro-discord（Discord 公用版）

**用途**: Discord 伺服器公用 Bot

**資料位置**: `./data/discord/.kuro/`

**存取權限**:
- ✅ 只能讀寫 `./data/discord/workspace`
- ⚠️ 禁止存取系統其他目錄
- ⚠️ 資源限制: 2 CPU, 4GB RAM

**啟動指令**:
```bash
docker-compose up -d kuro-discord

# 查看日誌
docker-compose logs -f kuro-discord
```

### ollama（可選）

**用途**: 本地 LLM 模型伺服器（兩個 Kuro 共用）

**端口**: `11434`

**GPU 支援**: 需要 NVIDIA GPU + nvidia-docker

**下載模型**:
```bash
# 進入 ollama 容器
docker exec -it kuro-ollama bash

# 下載模型
ollama pull qwen3:32b
ollama pull llama3.3:70b
ollama pull deepseek-r1

# 測試模型
ollama run qwen3:32b "Hello"
```

---

## Computer Use 與 Docker

> **重要：** Docker 容器預設沒有顯示器，桌面控制工具（`mouse_action`、`keyboard_action`、`computer_use`、`screenshot`）**無法在標準 Docker 容器中使用**。

### 方案 1：禁用桌面工具（推薦）

如果不需要 Computer Use，在容器配置中禁用相關工具：

```yaml
# data/discord/.kuro/config.yaml
security:
  disabled_tools:
    - mouse_action
    - keyboard_action
    - screen_info
    - computer_use
    - screenshot
    - clipboard_read
    - clipboard_write
```

### 方案 2：X11 轉發（進階）

如果需要在 Docker 中使用桌面控制，需掛載 X11 socket：

```yaml
services:
  kuro-desktop:
    build: .
    environment:
      - DISPLAY=${DISPLAY}
    volumes:
      - /tmp/.X11-unix:/tmp/.X11-unix:rw
      - kuro-data:/root/.kuro
    network_mode: host  # 或設定 X11 網路
```

在宿主機上允許 Docker 存取 X11：
```bash
xhost +local:docker
```

### 方案 3：虛擬顯示（Xvfb）

在 Dockerfile 中安裝 Xvfb，建立虛擬螢幕：

```dockerfile
RUN apt-get update && apt-get install -y xvfb
ENV DISPLAY=:99
CMD Xvfb :99 -screen 0 1920x1080x24 & poetry run kuro --web
```

> 此方案適合自動化測試或無人值守的 Computer Use 任務。

---

## 進階配置

### 1. 自訂配置檔

**個人版配置**:
```bash
# 建立個人版專用配置
mkdir -p data/personal/.kuro
cat > data/personal/.kuro/config.yaml <<EOF
llm:
  default: "anthropic/claude-sonnet-4.5"
  temperature: 0.7

security:
  auto_approve_levels: ["low", "medium"]  # 較寬鬆

sandbox:
  allowed_directories:
    - "/home/user/Documents"
    - "/home/user/Desktop"
EOF
```

**Discord 版配置**:
```bash
# 建立 Discord 版專用配置（更嚴格）
mkdir -p data/discord/.kuro
cat > data/discord/.kuro/config.yaml <<EOF
llm:
  default: "gemini/gemini-3-flash"  # 使用便宜的模型
  temperature: 0.5

security:
  auto_approve_levels: ["low"]  # 嚴格批准
  disabled_tools:
    - "shell_execute"  # 禁止執行 shell
    - "send_message"   # 禁止發送訊息給其他平台

sandbox:
  allowed_directories:
    - "/home/user/workspace"  # 只能存取工作區
  blocked_commands:
    - "*"  # 禁止所有 shell 指令

agents:
  - name: helper
    model: "ollama/qwen3:32b"  # 優先使用本地模型
    allowed_tools:
      - "file_read"
      - "file_list"
      - "get_time"
EOF
```

### 2. 資源限制調整

**編輯 docker-compose.yml**:

```yaml
services:
  kuro-discord:
    deploy:
      resources:
        limits:
          cpus: '4.0'      # 增加 CPU 配額
          memory: 8G       # 增加記憶體配額
        reservations:
          cpus: '1.0'
          memory: 1G
```

### 3. 多個 Discord Bot

**複製服務定義**:

```yaml
services:
  kuro-discord-server1:
    build: .
    container_name: kuro-discord-server1
    environment:
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN_SERVER1}
    volumes:
      - ./data/server1/.kuro:/app/data
    command: ["poetry", "run", "kuro", "discord"]

  kuro-discord-server2:
    build: .
    container_name: kuro-discord-server2
    environment:
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN_SERVER2}
    volumes:
      - ./data/server2/.kuro:/app/data
    command: ["poetry", "run", "kuro", "discord"]
```

**.env**:
```bash
DISCORD_BOT_TOKEN_SERVER1=token-for-server-1
DISCORD_BOT_TOKEN_SERVER2=token-for-server-2
```

---

## 管理指令

### 啟動/停止

```bash
# 啟動所有服務
docker-compose up -d

# 啟動特定服務
docker-compose up -d kuro-personal

# 停止所有服務
docker-compose down

# 停止並刪除資料（危險！）
docker-compose down -v
```

### 查看狀態

```bash
# 查看運行中的容器
docker-compose ps

# 查看日誌
docker-compose logs -f

# 查看特定服務日誌
docker-compose logs -f kuro-discord

# 查看資源使用
docker stats
```

### 更新 Kuro

```bash
# 1. 拉取最新程式碼
git pull

# 2. 重建映像檔
docker-compose build

# 3. 重啟服務
docker-compose down
docker-compose up -d
```

### 進入容器

```bash
# 進入個人版容器
docker exec -it kuro-personal bash

# 進入 Discord 版容器
docker exec -it kuro-discord bash

# 在容器內執行 Kuro CLI
docker exec -it kuro-personal poetry run kuro --help
```

### 備份資料

```bash
# 備份個人版資料
tar -czf kuro-personal-backup-$(date +%Y%m%d).tar.gz data/personal/.kuro

# 備份 Discord 版資料
tar -czf kuro-discord-backup-$(date +%Y%m%d).tar.gz data/discord/.kuro

# 恢復備份
tar -xzf kuro-personal-backup-20260216.tar.gz
```

---

## 隔離驗證

### 檔案系統隔離

```bash
# 在個人版中建立檔案
docker exec kuro-personal touch /app/data/personal-file.txt

# 在 Discord 版中檢查（應該看不到）
docker exec kuro-discord ls /app/data/
# 輸出: 不包含 personal-file.txt

# 驗證: 兩個容器的資料完全分離
ls data/personal/.kuro/  # 個人版資料
ls data/discord/.kuro/   # Discord 版資料（不同）
```

### 憑證隔離

```bash
# 個人版儲存憑證
docker exec kuro-personal poetry run kuro --store-credential
# Service: test
# Key: api_key
# Value: personal-secret-123

# Discord 版無法存取個人版的憑證
docker exec kuro-discord poetry run kuro --retrieve-credential test api_key
# 輸出: Not found
```

### 記憶體隔離

兩個容器的記憶體空間完全獨立，即使一個容器崩潰也不會影響另一個。

---

## 網路隔離（進階）

### 建立獨立網路

```yaml
services:
  kuro-personal:
    networks:
      - personal-network

  kuro-discord:
    networks:
      - discord-network

networks:
  personal-network:
    driver: bridge
  discord-network:
    driver: bridge
```

### 限制網路存取

```yaml
services:
  kuro-discord:
    networks:
      discord-network:
        ipv4_address: 172.20.0.2
    # 禁止存取特定網段
    cap_drop:
      - NET_RAW
```

---

## GPU 支援（本地模型加速）

### 需求

- NVIDIA GPU
- NVIDIA Docker Runtime

### 安裝 NVIDIA Docker

```bash
# Ubuntu/Debian
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update
sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker
```

### 驗證 GPU

```bash
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# 應該顯示 GPU 資訊
```

### 配置 Ollama 使用 GPU

已在 `docker-compose.yml` 中配置：

```yaml
ollama:
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```

---

## 疑難排解

### 問題 1: 容器無法啟動

```bash
# 查看詳細錯誤
docker-compose logs kuro-discord

# 檢查映像檔是否正確建立
docker images | grep kuro

# 重建映像檔
docker-compose build --no-cache
```

### 問題 2: GPU 不可用

```bash
# 檢查 NVIDIA 驅動
nvidia-smi

# 檢查 Docker GPU 支援
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# 重啟 Docker
sudo systemctl restart docker
```

### 問題 3: 資料遺失

```bash
# 檢查 volume 掛載
docker inspect kuro-personal | grep Mounts -A 20

# 確認資料目錄存在
ls -la data/personal/.kuro/
ls -la data/discord/.kuro/
```

### 問題 4: 端口衝突

```bash
# 修改 docker-compose.yml 中的端口映射
ports:
  - "7861:7860"  # 改用 7861

# 或停止佔用端口的程序
netstat -ano | findstr :7860
taskkill /PID <PID> /F
```

---

## 安全建議

### 1. 不要在容器間共用敏感資料

```bash
# ❌ 錯誤：共用配置檔
volumes:
  - ./shared/.kuro:/app/data  # 所有容器都用這個

# ✅ 正確：每個容器獨立資料
volumes:
  - ./data/personal/.kuro:/app/data  # 個人版
  - ./data/discord/.kuro:/app/data   # Discord 版
```

### 2. 限制 Discord 版權限

```yaml
# Discord 版應該更嚴格
security:
  auto_approve_levels: ["low"]  # 只自動批准低風險
  disabled_tools:
    - "shell_execute"
    - "send_message"
    - "file_delete"
```

### 3. 定期備份

```bash
# 每日備份腳本
#!/bin/bash
DATE=$(date +%Y%m%d)
tar -czf backup/kuro-personal-$DATE.tar.gz data/personal/.kuro
tar -czf backup/kuro-discord-$DATE.tar.gz data/discord/.kuro

# 保留最近 7 天的備份
find backup/ -name "*.tar.gz" -mtime +7 -delete
```

### 4. 監控資源使用

```bash
# 設定警報腳本
#!/bin/bash
USAGE=$(docker stats --no-stream --format "{{.CPUPerc}}" kuro-discord | sed 's/%//')
if (( $(echo "$USAGE > 80" | bc -l) )); then
    echo "Warning: Discord Bot CPU usage is ${USAGE}%"
    # 發送通知...
fi
```

---

## 總結

使用 Docker 隔離的優勢：

✅ **完全隔離** - 檔案、記憶體、網路、憑證
✅ **資源控制** - 限制 CPU、記憶體、GPU 使用
✅ **易於管理** - 一鍵啟動/停止/更新
✅ **可擴展** - 輕鬆新增更多實例
✅ **跨平台** - Windows、Linux、macOS 通用

相比 VM 的優勢：

- 💨 **更輕量** - 秒級啟動 vs 分鐘級啟動
- 💰 **更省資源** - 100MB vs 數 GB 記憶體
- 🔧 **更易維護** - Docker Compose 一鍵管理
- 🚀 **更高效能** - 近乎原生效能

立即開始使用 Docker 運行多個隔離的 Kuro 實例吧！
