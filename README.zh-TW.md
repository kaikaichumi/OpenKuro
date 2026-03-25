# Kuro（繁體中文）- Personal AI Assistant

Language: [English](README.md) | 繁體中文

> **Kuro** 是以隱私優先為核心的個人 AI 助手，支援多代理、多模型、桌面控制、訊息平台整合與 Web GUI。

---

## 專案定位

Kuro 提供一套可以長期運作的本地化 AI 助手架構，重點在：

- 隱私與安全可控
- 多模型彈性路由（雲端 + 本地）
- 多代理協作（主代理 / 子代理 / 團隊 / A2A）
- 可觀測（dashboard、稽核、分析、通知）
- 可維運（自我修復、更新、排程、回滾）

---

## 核心功能

- 多代理架構：Primary Agent Instances、Sub-Agent、Agent Teams、A2A
- 多模型支援：Anthropic / OpenAI / Gemini / Ollama / OpenAI-compatible（llama.cpp/vLLM）
- MCP Bridge：將外部 MCP server 工具橋接成原生工具
- Web GUI：分割視窗聊天、即時 dashboard、審批流程、i18n
- Scheduler + 主動通知：定時任務、結果推播到 Discord/Telegram
- 安全能力：Capability Tokens、Secret Broker、Data Firewall、稽核鏈
- 自動化維運：Self-update、診斷修復（`!fix` / `/diagnose`）、回滾腳本

---

## 架構與安全文件

- 系統架構：`docs/SYSTEM_ARCHITECTURE.md`
- 記憶體架構：`docs/MEMORY_ARCHITECTURE.md`
- Gateway 路線圖：`GATEWAY_ROADMAP.md`
- Gateway Phase 1 驗證：`docs/GATEWAY_PHASE1_VALIDATION.md`
- Gateway Phase 7 演練：`docs/GATEWAY_PHASE7_DRILL.md`
- Gateway 回滾腳本：`scripts/gateway_rollback.py`（建議先用 `--dry-run`）

---

## 安裝

### 前置需求

- Python 3.12+
- [Poetry](https://python-poetry.org/docs/#installation)
- （可選）[Ollama](https://ollama.ai/)：本地模型
- （可選）Playwright：`playwright install chromium`

### 基本安裝流程

```bash
git clone https://github.com/kaikaichumi/OpenKuro.git
cd OpenKuro

poetry install
poetry run kuro --init

cp .env.example .env
# 依需求填入 API key
```

---

## 快速啟動

### CLI

```bash
poetry run kuro
```

### Web GUI

```bash
poetry run kuro --web
# 預設 http://127.0.0.1:7860
```

### Discord

```bash
# .env 需設定 KURO_DISCORD_TOKEN
poetry run kuro --discord
```

### Telegram

```bash
# .env 需設定 KURO_TELEGRAM_TOKEN
poetry run kuro --telegram
```

---

## OpenAI OAuth（Web UI）

若你想用訂閱（Plus/Pro）流量而非 API 計費，可在 Web UI 使用 OpenAI OAuth 登入。  
完整環境變數與模型清單請參考英文版 [README.md](README.md) 的 OAuth 章節。

---

## 本地模型（Ollama / llama.cpp）

- Ollama：建議先 `ollama pull` 目標模型，再在 Kuro 模型清單切換
- llama.cpp / vLLM：使用 OpenAI-compatible endpoint，並在 `config.yaml` 指向 base URL
- 若實際執行模型與設定名稱不一致，請查看 log 的 `model_response`（含 `actual_model`）

---

## 常用指令

```bash
# 啟動
poetry run kuro
poetry run kuro --web
poetry run kuro --discord

# 更新
poetry run kuro --update

# 測試
poetry run pytest
```

---

## Scheduler 通知（重點）

- 任務完成會推送：`📋 Scheduled task completed: ...`
- 若任務結果為空，會改成提示你檢查 logs（避免只出現空白 Result）
- Scheduler 具備 in-flight 防重複觸發，避免同一任務重疊執行造成重複通知

---

## 進階開發與完整說明

本中文文件提供快速上手與維運重點。  
完整功能細節、所有設定欄位、完整工具清單請參考英文版：[README.md](README.md)。

