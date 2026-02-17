# Discord Bot 使用指南

完整的 Kuro Discord Bot 設定與使用教學。

---

## 目錄

- [快速開始](#快速開始)
- [建立 Discord Bot](#建立-discord-bot)
- [設定與部署](#設定與部署)
- [指令參考](#指令參考)
- [使用場景](#使用場景)
- [權限與安全](#權限與安全)
- [進階設定](#進階設定)
- [常見問題](#常見問題)

---

## 快速開始

### 前置需求

- 已安裝並設定好 Kuro（參考 [DEPLOYMENT.md](DEPLOYMENT.md)）
- Discord 帳號
- 有管理員權限的 Discord 伺服器（或建立新伺服器）

### 5 分鐘快速設定

```bash
# 1. 建立 Discord Bot（見下方詳細步驟）
# 2. 複製 Bot Token

# 3. 設定環境變數
echo 'KURO_DISCORD_TOKEN=your-bot-token-here' >> .env

# 4. 啟動 Discord Bot
poetry run kuro --discord

# 5. 在 Discord 伺服器中 @mention bot 開始對話！
```

---

## 建立 Discord Bot

### 步驟 1: 建立 Application

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications)
2. 點擊 **New Application**
3. 輸入名稱（例如：Kuro AI）
4. 點擊 **Create**

### 步驟 2: 建立 Bot

1. 左側選單選擇 **Bot**
2. 點擊 **Add Bot** → **Yes, do it!**
3. 在 **Token** 區塊點擊 **Reset Token** → **Copy**
4. **⚠️ 重要**：妥善保存這個 Token，不要分享給任何人！

### 步驟 3: 設定 Bot 權限

在 **Bot** 頁面中：

#### Privileged Gateway Intents
勾選以下選項：
- ✅ **MESSAGE CONTENT INTENT**（必須）
- ✅ **SERVER MEMBERS INTENT**（可選）
- ✅ **PRESENCE INTENT**（可選）

#### Bot Permissions
建議最小權限：
- ✅ Read Messages/View Channels
- ✅ Send Messages
- ✅ Send Messages in Threads
- ✅ Embed Links
- ✅ Attach Files
- ✅ Read Message History
- ✅ Add Reactions

### 步驟 4: 邀請 Bot 到伺服器

1. 左側選單選擇 **OAuth2** → **URL Generator**
2. **SCOPES** 勾選：
   - ✅ `bot`
3. **BOT PERMISSIONS** 勾選上述建議權限
4. 複製生成的 URL
5. 在瀏覽器開啟該 URL，選擇伺服器並授權

---

## 設定與部署

### 方法 1: 環境變數（推薦）

```bash
# .env 檔案
KURO_DISCORD_TOKEN=your-discord-bot-token-here
```

### 方法 2: config.yaml

編輯 `~/.kuro/config.yaml`：

```yaml
adapters:
  discord:
    enabled: true
    bot_token_env: "KURO_DISCORD_TOKEN"  # 環境變數名稱
    command_prefix: "!"  # 指令前綴
    approval_timeout: 120  # 工具核准等待時間（秒）
    max_message_length: 2000  # Discord 訊息長度限制

    # 白名單（可選，空陣列 = 允許所有）
    allowed_user_ids: []  # 例如: [123456789, 987654321]
    allowed_channel_ids: []  # 例如: [111222333, 444555666]
```

### 啟動 Bot

```bash
# 方法 1: 僅啟動 Discord Bot
poetry run kuro --discord

# 方法 2: 同時啟動多個 adapter
poetry run kuro --adapters  # Discord + Telegram + Web UI

# 方法 3: Discord + Web GUI
poetry run kuro --discord --web
```

**成功啟動的日誌**：
```
2026-02-15 10:30:45 [info] discord_starting bot_token=***abcd
2026-02-15 10:30:47 [info] discord_started bot_user=Kuro#1234 guild_count=3
```

---

## 指令參考

所有指令預設使用 `!` 前綴（可在 config.yaml 修改）。

### 基本指令

#### `!help`
顯示所有可用指令和使用說明。

```
!help
```

**回應範例**：
```
🏔 Kuro AI Assistant

Commands:
!help — Show this help
!model — Show current model
!model <name> — Switch AI model
!models — List available models
!agents — List available sub-agents
!delegate <agent> <task> — Delegate task to a sub-agent
!clear — Clear conversation history
!trust — Show/set trust level

Usage:
- In DMs: just type your message
- In servers: mention me or use commands
```

---

### 模型管理

#### `!model`
顯示當前使用的模型。

```
!model
```

**回應範例**：
```
🤖 Current model: anthropic/claude-sonnet-4.5
```

#### `!model <模型名稱>`
切換到指定的模型。

```
!model ollama/qwen3:32b
!model anthropic/claude-opus-4.6
!model openai/gpt-5.2
!model gemini/gemini-3-flash
```

**回應範例**：
```
✅ Model switched to: ollama/qwen3:32b
```

**注意事項**：
- 每個使用者在每個頻道有獨立的模型設定
- 切換模型不會清除對話歷史
- 確保模型名稱正確（使用 `!models` 查看）

#### `!models`
列出所有可用的模型，當前使用的模型會標示 ✅。

```
!models
```

**回應範例**：
```
**Available models:**

**Gemini:**
  `gemini/gemini-3-flash`
  `gemini/gemini-3-pro`

**Anthropic:**
  `anthropic/claude-opus-4.6`
  `anthropic/claude-sonnet-4.5` ✅
  `anthropic/claude-haiku-4.5`

**OpenAI:**
  `openai/gpt-5.3-codex`
  `openai/gpt-5.2`
  `openai/gpt-5`

**Ollama:**
  `ollama/qwen3:32b`
  `ollama/qwen3-coder`
  `ollama/llama3.3:70b`
```

---

### Sub-Agent 管理

#### `!agents`
列出所有已註冊的 sub-agents（子代理），顯示它們的模型、工具限制等資訊。

```
!agents
```

**回應範例**：
```
🤖 **Available Sub-Agents:**

  **fast**
    Model: `ollama/qwen3:32b`
    Rounds: 3

  **coder**
    Model: `ollama/qwen3-coder`
    Rounds: 5
    Tools: file_read, file_write, file_search, shell_execute

  **cloud**
    Model: `anthropic/claude-sonnet-4.5`
    Rounds: 10

⚡ Running: 0

**Usage:** `!delegate <agent_name> <task>`
```

**說明**：
- **Rounds**：agent 最多可執行幾輪工具呼叫
- **Tools**：agent 可使用的工具（空白表示可用全部工具）
- **Running**：目前正在執行的 agent 數量

#### `!delegate <agent> <task>`
直接將任務委派給指定的 sub-agent 執行，**繞過主 LLM，保證使用指定模型**。

```
!delegate <agent_name> <task_description>
```

**範例**：
```
!delegate coder 幫我寫一個快速排序函數

⚡ Delegating to **coder** (model: `ollama/qwen3-coder`)...

📨 **coder** responded:

這是一個 Python 快速排序實作：

​```python
def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quicksort(left) + middle + quicksort(right)
​```

時間複雜度：平均 O(n log n)，最差 O(n²)
```

**為什麼使用 `!delegate`？**
1. **保證模型執行**：不依賴主 LLM 判斷，直接用指定模型
2. **更快回應**：跳過主 LLM 的「思考」階段
3. **明確控制**：你確切知道哪個模型在處理任務
4. **GPU 驗證**：本地模型（如 `ollama/qwen3-coder`）會真正在 GPU 上跑

**與主 LLM 委派的差異**：
```
# 方法 1: 主 LLM 自動委派（可能會假裝）
@Kuro 請 coder 幫我寫排序函數
→ 主 LLM 決定是否呼叫 delegate_to_agent 工具
→ 有時會假裝委派，實際自己回答

# 方法 2: !delegate 直接委派（保證執行）✅
!delegate coder 幫我寫排序函數
→ 100% 使用 ollama/qwen3-coder 模型
→ GPU 會跑，可用 nvidia-smi 或 ollama ps 驗證
```

**檢查 GPU 是否在跑**：
```bash
# Terminal 1: 啟動 Discord bot
poetry run kuro --discord

# Terminal 2: 監控 GPU
nvidia-smi -l 1      # NVIDIA GPU（每秒刷新）
# 或
ollama ps             # 查看 Ollama 正在執行的模型
```

當你執行 `!delegate coder ...` 時，應該會看到 `qwen3-coder` 模型出現在 GPU 上。

---

### 對話管理

#### `!clear`
清除當前對話歷史，重新開始。

```
!clear
```

**回應範例**：
```
🗑 Conversation cleared. Starting fresh!
```

**注意**：
- 清除後模型設定保持不變
- 信任等級會重置
- 無法復原

---

### 安全與信任

#### `!trust`
顯示當前信任等級。

```
!trust
```

**回應範例**：
```
🔒 Current trust level: LOW
Usage: !trust low|medium|high|critical
```

#### `!trust <等級>`
設定信任等級，控制工具自動核准範圍。

```
!trust low      # 僅自動核准 LOW 風險工具
!trust medium   # 自動核准 LOW + MEDIUM
!trust high     # 自動核准 LOW + MEDIUM + HIGH
!trust critical # 自動核准所有工具（危險！）
```

**回應範例**：
```
🔓 Trust level set to: MEDIUM
```

**風險等級說明**：

| 等級 | 自動核准工具範例 | 建議場景 |
|------|----------------|---------|
| **LOW** | file_read, screenshot, memory_search, get_time, screen_info | 一般使用（預設） |
| **MEDIUM** | file_write, clipboard_write, web_navigate, mouse_action, keyboard_action | 信任的使用者 |
| **HIGH** | shell_execute, computer_use | 開發環境、受控環境 |
| **CRITICAL** | send_message | 不建議使用 |

---

## 使用場景

### 場景 1: 在伺服器頻道中使用

**在公開頻道中，需要 mention bot**：

```
User: @Kuro 幫我解釋什麼是遞迴

Bot: 遞迴（Recursion）是指函數呼叫自己的程式設計技巧...
```

**指令不需要 mention**：
```
User: !model ollama/qwen3:32b

Bot: ✅ Model switched to: ollama/qwen3:32b

User: @Kuro 現在用中文回答會更好嗎？

Bot: 是的！Qwen3 對中文的理解和生成能力非常出色...
```

### 場景 2: 在 DM（私訊）中使用

**DM 中直接輸入，不需要 mention**：

```
User: 你好，幫我寫一個 Python 排序函數

Bot: 當然！這裡是一個快速排序的實作...
```

### 場景 3: 工具核准流程

當 bot 需要執行高風險操作時：

```
User: @Kuro 幫我建立一個新檔案 test.txt

Bot: ⚡ Approval Required

     Tool: file_write
     Risk: ⚠️ MEDIUM
     Params:
       path: test.txt
       content: Hello World

     [Allow] [Deny] [Trust this level]
```

點擊按鈕做出選擇：
- **Allow**：僅核准這次操作
- **Deny**：拒絕操作
- **Trust this level**：自動核准此等級（MEDIUM）的所有工具

### 場景 4: 模型策略（成本優化）

**日常對話用便宜模型**：
```
!model gemini/gemini-3-flash  # 便宜快速

User: @Kuro 今天天氣如何？

Bot: [使用 Gemini 3 Flash 回答...]
```

**複雜任務切換到強大模型**：
```
!model anthropic/claude-opus-4.6  # 最強推理

User: @Kuro 幫我設計一個微服務架構...

Bot: [使用 Claude Opus 4.6 深度分析...]
```

**程式碼任務用專門模型**：
```
!model openai/gpt-5.3-codex  # 程式碼專用

User: @Kuro 重構這段程式碼...

Bot: [使用 GPT-5.3-Codex 優化...]
```

### 場景 5: Sub-Agent 委派（保證使用指定模型）

**問題：主 LLM 假裝委派**
```
User: @Kuro 請 coder 幫我寫一個排序函數

Bot: 好的，我請 Kuro-Coder 為您處理...

     這是排序函數：
     [主 agent 自己回答，coder 根本沒跑]
```
❌ GPU 沒在跑，因為主 agent 只是假裝委派

**解決方案：使用 `!delegate` 直接委派**
```
User: !delegate coder 寫一個快速排序函數

Bot: ⚡ Delegating to **coder** (model: `ollama/qwen3-coder`)...

     📨 **coder** responded:

     ​```python
     def quicksort(arr):
         if len(arr) <= 1:
             return arr
         pivot = arr[len(arr) // 2]
         ...
     ​```
```
✅ **保證** `ollama/qwen3-coder` 在 GPU 上執行

**驗證模型真的在跑**：
```bash
# 另開終端
ollama ps

NAME              ID         SIZE    PROCESSOR    UNTIL
qwen3-coder:latest a1b2c3d4   4.7 GB  100% GPU     4 minutes from now
```

### 場景 6: Computer Use（桌面控制）

透過 Discord 指揮 Kuro 操控本機桌面：

```
User: @Kuro 幫我打開 Chrome 然後搜尋今天的天氣

Bot: 好的，我來操控你的電腦完成這個任務。

     ⚡ Approval Required
     Tool: computer_use
     Risk: ⚠️ HIGH
     Params: task="打開 Chrome 搜尋今天的天氣"
     [Allow] [Deny]

User: [點擊 Allow]

Bot: [截圖] 目前畫面：桌面
     → keyboard_action(hotkey, ["win","r"])
     [截圖] 執行對話框已開啟
     → keyboard_action(type, "chrome")
     → keyboard_action(press, "enter")
     [截圖] Chrome 已開啟
     → keyboard_action(type, "今天天氣")
     → keyboard_action(press, "enter")
     [截圖] 搜尋結果

     完成！以下是搜尋結果截圖。
```

> **注意：** Computer Use 需要 Kuro 運行在有顯示器的環境（非 Docker headless）。
> 建議使用支援 Vision 的模型（Claude Sonnet/Opus, GPT-4o）以獲得最佳效果。

**安全建議：** 在 Discord 環境中建議對桌面控制工具保持嚴格審批：

```yaml
security:
  require_approval_for:
    - computer_use
    - mouse_action
    - keyboard_action
```

### 場景 7: 本地模型（完全離線）

```
!model ollama/qwen3:32b

User: @Kuro 你現在是用本地模型嗎？

Bot: 是的！我現在運行在本地的 Qwen3 32B 模型上，
     你的資料完全不會離開這台機器。
```

> **Computer Use + 本地模型：** 若要使用 Computer Use，需選用支援 Vision 的本地模型（如 `ollama/llava`）。純文字模型無法分析截圖。

---

## 權限與安全

### 使用者白名單

限制只有特定使用者可以使用 bot：

```yaml
# config.yaml
adapters:
  discord:
    allowed_user_ids:
      - 123456789012345678  # User A
      - 987654321098765432  # User B
```

**如何取得 User ID**：
1. Discord 設定 → 進階 → 開啟「開發者模式」
2. 右鍵點擊使用者 → 「複製使用者 ID」

### 頻道白名單

限制 bot 只在特定頻道回應：

```yaml
# config.yaml
adapters:
  discord:
    allowed_channel_ids:
      - 111222333444555666  # #ai-assistant
      - 777888999000111222  # #dev-tools
```

**如何取得 Channel ID**：
1. Discord 設定 → 進階 → 開啟「開發者模式」
2. 右鍵點擊頻道 → 「複製頻道 ID」

### 禁用特定工具

完全禁用某些工具（例如在公開伺服器）：

```yaml
# config.yaml
security:
  disabled_tools:
    - shell_execute     # 禁用 Shell 執行
    - send_message      # 禁用發送訊息
    - file_write        # 禁用檔案寫入
```

### 工具核准逾時

設定使用者回應核准請求的等待時間：

```yaml
# config.yaml
adapters:
  discord:
    approval_timeout: 120  # 120 秒（2 分鐘）
```

超過時間未回應，自動拒絕操作。

### 預定義 Sub-Agents

在 config.yaml 中預先定義 sub-agents，重啟後立即可用：

```yaml
# config.yaml
agents:
  enabled: true
  max_concurrent_agents: 5
  predefined:
    # 快速本地 agent（處理簡單任務）
    - name: fast
      model: ollama/qwen3:32b
      system_prompt: "You are a fast assistant. Be extremely concise."
      max_tool_rounds: 3

    # 程式碼專用 agent
    - name: coder
      model: ollama/qwen3-coder
      system_prompt: "You are a coding specialist. Focus on clean, efficient code."
      allowed_tools:
        - file_read
        - file_write
        - file_search
        - shell_execute
      max_tool_rounds: 5

    # 雲端推理 agent（複雜任務）
    - name: thinker
      model: anthropic/claude-sonnet-4.5
      system_prompt: "You handle complex reasoning and analysis."
      max_tool_rounds: 10
```

**重啟 bot 後**：
```
!agents

🤖 **Available Sub-Agents:**

  **fast**
    Model: `ollama/qwen3:32b`
    Rounds: 3

  **coder**
    Model: `ollama/qwen3-coder`
    Rounds: 5
    Tools: file_read, file_write, file_search, shell_execute

  **thinker**
    Model: `anthropic/claude-sonnet-4.5`
    Rounds: 10
```

---

## 進階設定

### Session 隔離機制

Kuro Discord bot 使用 `頻道 ID + 使用者 ID` 作為 session key：

```
Channel: #general
  User A session: 111222333:123456789 (獨立對話、獨立模型)
  User B session: 111222333:987654321 (獨立對話、獨立模型)

Channel: #tech
  User A session: 444555666:123456789 (與 #general 中的 A 分離)
```

**實際影響**：
- 你在 `#general` 設定 `!model ollama/qwen3:32b`
- 不會影響你在 `#tech` 的模型設定
- 也不會影響其他使用者的設定

### 訊息分割

Discord 單則訊息限制 2000 字元，Kuro 會自動智慧分割：

**優先順序**：
1. 程式碼區塊邊界（` ``` ` 標記）
2. 段落（雙換行）
3. 行（單換行）
4. 詞彙（空格）
5. 字元（最後手段）

```yaml
# config.yaml
adapters:
  discord:
    max_message_length: 2000  # 預設值，不建議修改
```

### 多 Bot 同時運行

可以同時運行多個 Kuro bot 實例（不同 Token）：

```bash
# Terminal 1: Bot A (GPT-5 專用)
KURO_DISCORD_TOKEN=token-A poetry run kuro --discord

# Terminal 2: Bot B (本地模型專用)
KURO_DISCORD_TOKEN=token-B poetry run kuro --discord
```

### 自訂指令前綴

```yaml
# config.yaml
adapters:
  discord:
    command_prefix: "/"  # 使用 /model 而非 !model
```

**注意**：避免使用 Discord 內建的 `/` slash commands 前綴，會有衝突。

---

## 常見問題

### Q: Bot 上線但不回應？

**檢查清單**：

1. **確認 MESSAGE CONTENT INTENT 已啟用**
   - Discord Developer Portal → Bot → Privileged Gateway Intents

2. **檢查是否需要 mention**
   - 伺服器頻道：需要 `@Kuro`
   - DM：不需要 mention

3. **確認頻道權限**
   - Bot 需要「讀取訊息」和「發送訊息」權限

4. **檢查白名單設定**
   ```yaml
   allowed_user_ids: []  # 空陣列 = 允許所有
   allowed_channel_ids: []
   ```

### Q: 工具核准按鈕點擊無反應？

**原因**：按鈕可能已過期（approval_timeout）。

**解決**：
1. 重新執行指令
2. 增加 timeout 時間：
   ```yaml
   adapters:
     discord:
       approval_timeout: 300  # 5 分鐘
   ```

### Q: 如何重置對話但保留模型設定？

```
!clear  # 只清除對話，模型設定保留
```

如果要完全重置：
```
!clear
!model  # 確認模型是否正確
```

### Q: Bot 回應被截斷？

**原因**：超過 Discord 2000 字元限制。

**Kuro 會自動分割訊息**，如果仍有問題：
1. 要求 bot 簡短回答
2. 使用 `!model` 切換到更簡潔的模型
3. 將複雜問題拆分成多個小問題

### Q: 如何在多個伺服器使用同一個 Bot？

**答**：Bot Token 可以在多個伺服器使用，每個伺服器的頻道/使用者有獨立 session。

```bash
# 單一 bot 實例可同時服務多個伺服器
poetry run kuro --discord
```

如果要限制特定伺服器：
```yaml
allowed_channel_ids:
  - 111222333  # 伺服器 A 的 #ai 頻道
  - 444555666  # 伺服器 B 的 #bot 頻道
```

### Q: 為什麼 `@Kuro 請 coder 處理` 沒有用本地模型？

**問題**：主 LLM 會「假裝」委派，實際上自己回答。

**原因**：
- 主 LLM 看到「請 coder 處理」這樣的文字
- 它理解意圖，但可能選擇不呼叫 `delegate_to_agent` 工具
- 而是假裝說「好的，coder 回覆如下...」然後自己答

**解決方案**：
```
# ❌ 不可靠（主 LLM 可能假裝）
@Kuro 請 coder 幫我寫程式

# ✅ 100% 可靠（直接委派）
!delegate coder 幫我寫程式
```

### Q: 如何確認 GPU 正在執行本地模型？

**方法 1: nvidia-smi（NVIDIA GPU）**
```bash
# 開另一個終端，持續監控
nvidia-smi -l 1

# 執行委派後應該看到
+-----------------------------------------------------------------------------+
| Processes:                                                                  |
|  GPU   GI   CI        PID   Type   Process name                  GPU Memory |
|        ID   ID                                                   Usage      |
|=============================================================================|
|    0   N/A  N/A      12345    C   ...ollama                        4567MiB |
+-----------------------------------------------------------------------------+
```

**方法 2: ollama ps（Ollama）**
```bash
ollama ps

NAME              ID         SIZE    PROCESSOR    UNTIL
qwen3-coder:latest a1b2c3d4   4.7 GB  100% GPU     4 minutes from now
```

如果看到模型出現，GPU 就是在跑！

**方法 3: 檢查回應時間**
```
本地 7B 模型（GPU）：2-4 秒
雲端 API（網路）：1-3 秒（通常更快，但有網路延遲）
本地模型（CPU only）：10-30 秒（非常慢）
```

### Q: 本地模型速度太慢怎麼辦？

**策略 1：切換到更小的模型**
```
!model ollama/llama3.2:3b  # 3B 模型，速度快
```

**策略 2：混合使用**
```
# 簡單任務用本地
!model ollama/qwen3:32b
@Kuro 什麼是遞迴？

# 複雜任務用雲端
!model anthropic/claude-sonnet-4.5
@Kuro 幫我設計系統架構...
```

**策略 3：GPU 加速**
- 確認 Ollama 使用 GPU（NVIDIA/AMD/Apple Metal）
- 檢查：`ollama ps` 應該顯示 GPU 使用

### Q: 如何監控 Bot 狀態？

```bash
# 查看日誌
tail -f ~/.kuro/logs/assistant.log

# 或啟動時直接在終端看
poetry run kuro --discord
```

在 Discord 中測試：
```
!model  # 確認 bot 回應
!models # 確認模型列表正常
```

### Q: Bot 需要哪些 Discord 權限？

**最小權限集**：
```
✅ Read Messages/View Channels
✅ Send Messages
✅ Embed Links (用於格式化訊息)
✅ Read Message History (載入上下文)
```

**建議權限**（完整功能）：
```
✅ 上述所有權限
✅ Send Messages in Threads
✅ Attach Files (如果 bot 需要傳送檔案)
✅ Add Reactions (未來可能用於互動)
```

---

## 最佳實踐

### 1. 成本優化策略

```yaml
# 設定便宜模型為預設
models:
  default: "gemini/gemini-3-flash"  # 便宜快速

  fallback_chain:
    - "gemini/gemini-3-flash"
    - "ollama/qwen3:32b"  # 免費本地
    - "anthropic/claude-sonnet-4.5"  # 複雜任務才用
```

**使用時機**：
- 閒聊、簡單問答 → Gemini Flash / 本地模型
- 程式碼生成 → GPT-5.3-Codex / Qwen3-Coder
- 深度分析 → Claude Opus 4.6

### 2. 安全設定（公開伺服器）

```yaml
security:
  auto_approve_levels: []  # 所有工具都要核准
  disabled_tools:
    - shell_execute
    - file_write
    - send_message

adapters:
  discord:
    allowed_user_ids: [123456789]  # 僅管理員
```

### 3. 效能優化（大型伺服器）

```yaml
models:
  default: "ollama/qwen3:32b"  # 本地模型減少 API 延遲

adapters:
  discord:
    approval_timeout: 60  # 縮短等待時間
```

### 4. 多環境部署

```bash
# 開發環境：使用本地模型
KURO_HOME=~/.kuro-dev poetry run kuro --discord

# 生產環境：使用雲端模型
KURO_HOME=~/.kuro-prod poetry run kuro --discord
```

---

## 相關資源

- **Kuro 主文檔**：[README.md](../README.md)
- **部署指南**：[DEPLOYMENT.md](DEPLOYMENT.md)
- **系統提示加密**：[SYSTEM_PROMPT_ENCRYPTION.md](SYSTEM_PROMPT_ENCRYPTION.md)
- **Discord Developer Portal**：https://discord.com/developers/applications
- **discord.py 文檔**：https://discordpy.readthedocs.io/

---

## 取得協助

遇到問題？

1. 查看日誌：`~/.kuro/logs/assistant.log`
2. 測試基本指令：`!help`, `!model`, `!models`
3. 確認 config 設定：`~/.kuro/config.yaml`
4. 提交 Issue 到 GitHub 倉庫

---

**祝你使用愉快！🚀**
