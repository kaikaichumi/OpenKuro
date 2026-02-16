# 排程系統快速上手

5 分鐘學會使用 Kuro 的排程系統！

---

## 1️⃣ 啟動 Discord Bot（排程器自動啟動）

```bash
poetry run kuro discord
```

輸出：
```
Kuro adapters running: discord
Task scheduler started  ← 排程器已啟動
Press Ctrl+C to stop.
```

---

## 2️⃣ 在 Discord 中建立第一個排程

### 範例：每天早上 9 點查股票

```
你: @Kuro 幫我設定每天早上 9 點查詢 AAPL 股票價格

Kuro: 好的，我來為你建立排程...
[呼叫 schedule_add 工具]

✅ Scheduled task 'AAPL Daily Monitor'

Tool: get_stock_price
Parameters: {"symbol": "AAPL"}
Schedule: Daily at 09:00
Next run: 2026-02-17 09:00:00
```

---

## 3️⃣ 查看所有排程

```
你: @Kuro 顯示所有排程任務

Kuro:
📅 Scheduled Tasks

1. AAPL Daily Monitor (✅ Enabled)
   ID: aapl-daily-monitor
   Tool: get_stock_price
   Schedule: Daily at 09:00
   Next run: 2026-02-17 09:00
   Last run: Never
   Run count: 0
```

---

## 4️⃣ 等待排程自動執行

明天早上 9 點，Kuro 會自動：
1. 呼叫 `get_stock_price` 工具
2. 查詢 AAPL 股價
3. 記錄結果到 Audit Log

如果在 Discord 中，可以設定 Plugin 自動發送結果到頻道！

---

## 5️⃣ 管理排程

### 停用排程
```
你: @Kuro 停用 aapl-daily-monitor 排程

Kuro: ⏸️ Disabled task 'aapl-daily-monitor'
```

### 重新啟用
```
你: @Kuro 啟用 aapl-daily-monitor 排程

Kuro: ✅ Enabled task 'aapl-daily-monitor'
Next run: 2026-02-17 09:00
```

### 刪除排程
```
你: @Kuro 刪除 aapl-daily-monitor 排程

Kuro: ✅ Removed scheduled task 'aapl-daily-monitor'
```

---

## 📚 更多範例

### 每週報告
```
你: 設定每週一早上 10 點生成週報

schedule_add(
    task_id="weekly-report",
    name="Weekly Report",
    tool_name="generate_report",
    schedule_type="weekly",
    schedule_time="10:00",
    schedule_days=[0]  # 0 = Monday
)
```

### 每小時檢查
```
你: 每小時檢查一次伺服器狀態

schedule_add(
    task_id="server-check-hourly",
    name="Hourly Server Check",
    tool_name="check_server",
    schedule_type="hourly"
)
```

### 每 30 分鐘
```
你: 每 30 分鐘監控 CPU 使用率

schedule_add(
    task_id="cpu-monitor",
    name="CPU Monitor",
    tool_name="monitor_cpu",
    schedule_type="interval",
    interval_minutes=30
)
```

---

## 🎯 實用技巧

### 1. 使用有意義的 task_id
```
✅ "stock-aapl-daily"
✅ "backup-weekly"
✅ "news-morning"

❌ "task1"
❌ "abc"
❌ "temp"
```

### 2. 先測試工具
```bash
# 1. 手動執行測試
你: 執行 get_stock_price 工具，參數 symbol=AAPL

# 2. 確認正常後再排程
你: 好，設定每天早上 9 點執行
```

### 3. 查看執行歷史
```bash
# 在終端查詢 audit log
kuro --audit-query --tool get_stock_price --limit 10

# 會顯示所有執行記錄
2026-02-16 09:00:15 | get_stock_price | {"symbol": "AAPL"} | auto
2026-02-15 09:00:10 | get_stock_price | {"symbol": "AAPL"} | auto
...
```

---

## 🔧 進階：委派給專家 Agent

結合 Multi-Agent 系統，讓專家 Agent 處理排程任務！

### 配置 scheduler agent

```yaml
# ~/.kuro/config.yaml
agents:
  definitions:
    - name: scheduler
      model: gemini/gemini-3-flash
      system_prompt: "You are a task scheduler specialist."
      allowed_tools:
        - schedule_add
        - schedule_list
        - schedule_remove
        - get_time
```

### 使用

```
你: @Kuro 委派給 scheduler，幫我設定每天早上 8 點、中午 12 點、下午 6 點查詢 TSLA 股價

Kuro: [委派給 scheduler agent]

Scheduler Agent:
我已為你設定 3 個排程任務：

1. TSLA Morning Check (08:00)
2. TSLA Noon Check (12:00)
3. TSLA Evening Check (18:00)

所有任務都已啟用，明天開始自動執行。
```

---

## ❓ 常見問題

### Q: 排程會在重啟後保留嗎？
**A**: 是的！儲存在 `~/.kuro/scheduler.json`，重啟自動載入。

### Q: 可以排程執行 Skill 嗎？
**A**: 可以！只要建立一個呼叫 Skill 的工具即可。

### Q: 排程失敗怎麼辦？
**A**: 查看 `~/.kuro/logs/assistant.log` 和 audit log，會記錄錯誤。

### Q: 如何接收排程結果通知？
**A**:
- Discord: Plugin 可以發送訊息到頻道
- 日誌: 查詢 audit log
- 檔案: 讓工具寫入結果到檔案

---

## 📖 完整文件

詳細文件請參考：
- [Scheduler Guide](docs/SCHEDULER_GUIDE.md) - 完整排程系統說明
- [Config Example](config.example.yaml) - 配置範例

---

開始使用 Kuro 排程系統，自動化你的日常任務吧！🚀
