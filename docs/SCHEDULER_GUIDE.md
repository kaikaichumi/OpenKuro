# Kuro 排程系統使用指南

Kuro 內建了一個強大的任務排程系統，可以讓工具在特定時間自動執行。

---

## 快速開始

### 1. 啟動排程系統

排程系統在以下模式會自動啟動：
- Discord Bot 模式
- Telegram Bot 模式
- 所有 Adapter 模式

```bash
# 啟動 Discord Bot（排程器自動啟動）
poetry run kuro discord
```

### 2. 添加第一個排程任務

```bash
# 在 Discord 中
> 幫我設定每天早上 9 點查詢股票價格

# Kuro 會自動呼叫 schedule_add 工具
✅ Scheduled task 'stock-monitor-daily' (ID: stock-monitor)

Tool: get_stock_price
Schedule: Daily at 09:00
Next run: 2026-02-17 09:00
```

### 3. 查看所有排程

```bash
> 顯示所有排程任務

📅 Scheduled Tasks

1. Stock Monitor (✅ Enabled)
   ID: stock-monitor
   Tool: get_stock_price
   Schedule: Daily at 09:00
   Next run: 2026-02-17 09:00
   Last run: 2026-02-16 09:00
   Run count: 5
```

---

## 排程類型

### 1. Daily（每天）

每天在指定時間執行一次。

**範例**：
```
> 設定每天早上 10:30 備份資料

# 工具呼叫
schedule_add(
    task_id="backup-daily",
    name="Daily Backup",
    tool_name="backup_tool",
    schedule_type="daily",
    schedule_time="10:30"
)
```

### 2. Weekly（每週）

每週在指定的幾天執行。

**範例**：
```
> 設定每週一、三、五早上 9 點執行股票分析

# 工具呼叫
schedule_add(
    task_id="stock-analysis-weekly",
    name="Weekly Stock Analysis",
    tool_name="analyze_stocks",
    schedule_type="weekly",
    schedule_time="09:00",
    schedule_days=[0, 2, 4]  # 0=Monday, 2=Wednesday, 4=Friday
)
```

**星期對照**：
- 0 = Monday（週一）
- 1 = Tuesday（週二）
- 2 = Wednesday（週三）
- 3 = Thursday（週四）
- 4 = Friday（週五）
- 5 = Saturday（週六）
- 6 = Sunday（週日）

### 3. Hourly（每小時）

每小時執行一次（在整點執行）。

**範例**：
```
> 設定每小時檢查系統狀態

schedule_add(
    task_id="status-check-hourly",
    name="Hourly Status Check",
    tool_name="check_status",
    schedule_type="hourly"
)
```

### 4. Interval（間隔）

每隔 N 分鐘執行一次。

**範例**：
```
> 設定每 30 分鐘監控伺服器

schedule_add(
    task_id="server-monitor",
    name="Server Monitor",
    tool_name="monitor_server",
    schedule_type="interval",
    interval_minutes=30
)
```

### 5. Once（一次性）

在指定時間執行一次後自動停用。

**範例**：
```
> 設定明天早上 8 點提醒我開會

schedule_add(
    task_id="meeting-reminder",
    name="Meeting Reminder",
    tool_name="send_notification",
    schedule_type="once",
    schedule_time="08:00"
)
```

---

## 管理排程任務

### 查看所有任務

```bash
> 列出所有排程

# 或直接呼叫工具
schedule_list()
```

### 停用任務

```bash
> 停用 stock-monitor 排程

schedule_disable(task_id="stock-monitor")

⏸️ Disabled task 'stock-monitor'
```

### 啟用任務

```bash
> 啟用 stock-monitor 排程

schedule_enable(task_id="stock-monitor")

✅ Enabled task 'stock-monitor'
Next run: 2026-02-17 09:00
```

### 刪除任務

```bash
> 刪除 backup-daily 排程

schedule_remove(task_id="backup-daily")

✅ Removed scheduled task 'backup-daily'
```

---

## 使用範例

### 範例 1: 股票監控系統

```yaml
# 配置文件中定義 agent
agents:
  enabled: true
  definitions:
    - name: stock-analyst
      model: anthropic/claude-sonnet-4.5
      system_prompt: "You are a stock market analyst."
      allowed_tools:
        - get_stock_price
        - schedule_add
        - schedule_list
```

**使用**:
```
使用者: 幫我設定每天早上 9 點和下午 3 點監控 AAPL 和 TSLA 股票

Kuro:
✅ 已設定兩個排程任務：

1. 早盤監控（每天 09:00）
   - AAPL
   - TSLA

2. 收盤監控（每天 15:00）
   - AAPL
   - TSLA

您可以在 Discord 頻道中接收自動更新。
```

### 範例 2: 定時備份

```
使用者: 設定每週日晚上 11 點備份我的資料

Kuro 呼叫:
schedule_add(
    task_id="weekly-backup",
    name="Weekly Backup",
    tool_name="backup_files",
    parameters={
        "source": "~/Documents",
        "destination": "~/Backups"
    },
    schedule_type="weekly",
    schedule_time="23:00",
    schedule_days=[6]  # Sunday
)

✅ Scheduled task 'Weekly Backup'
Schedule: Weekly on Sun at 23:00
Next run: 2026-02-23 23:00
```

### 範例 3: 新聞摘要

```
使用者: 每天早上 8 點幫我抓取科技新聞摘要

Kuro 呼叫:
schedule_add(
    task_id="tech-news-daily",
    name="Daily Tech News",
    tool_name="fetch_news",
    parameters={
        "category": "technology",
        "count": 10
    },
    schedule_type="daily",
    schedule_time="08:00"
)

✅ Scheduled task 'Daily Tech News'
每天早上 8 點我會自動抓取 10 則科技新聞。
```

---

## 排程結果通知

### Discord 通知（推薦）

如果使用 Discord Bot 模式，可以讓排程任務自動發送結果到頻道。

**修改 Plugin**:
```python
# 在你的 plugin 中加入 Discord 通知
class StockMonitorTool(BaseTool):
    async def execute(self, symbols: list[str]) -> str:
        results = []
        for symbol in symbols:
            price = await self.get_price(symbol)
            results.append(f"{symbol}: ${price}")

        # 如果在 Discord 環境中，結果會自動發送到頻道
        return "\n".join(results)
```

### 日誌記錄

所有排程執行都會記錄到 Audit Log：

```bash
# 查詢排程執行歷史
kuro --audit-query --tool schedule_add --limit 20

2026-02-16 09:00:15 | schedule_add | {"task_id": "stock-monitor", ...} | executed
2026-02-16 09:00:30 | get_stock_price | {"symbol": "AAPL"} | auto
```

---

## 配置檔案

排程任務儲存在 `~/.kuro/scheduler.json`:

```json
{
  "tasks": [
    {
      "id": "stock-monitor",
      "name": "Stock Monitor",
      "tool_name": "get_stock_price",
      "parameters": {
        "symbol": "AAPL"
      },
      "schedule_type": "daily",
      "schedule_time": "09:00",
      "enabled": true,
      "last_run": "2026-02-16T09:00:00",
      "next_run": "2026-02-17T09:00:00",
      "run_count": 5,
      "created_at": "2026-02-10T10:30:00"
    }
  ],
  "updated_at": "2026-02-16T09:00:15"
}
```

**手動編輯**：可以直接編輯此檔案修改排程，重啟 Kuro 後生效。

---

## 最佳實踐

### 1. 使用有意義的 task_id

```
✅ 好：stock-monitor-daily, backup-weekly, news-morning
❌ 壞：task1, abc, temp
```

### 2. 設定合適的間隔

```
✅ 每 30 分鐘檢查一次（合理）
❌ 每 1 分鐘檢查一次（太頻繁，可能影響效能）
```

### 3. 考慮時區

Kuro 使用系統本地時間。如果伺服器和使用者在不同時區，需要注意。

```python
# 檢查當前時間
> 現在幾點？

Date: 2026-02-16
Time: 14:30:00
Day: Sunday
Timezone: UTC+8
```

### 4. 測試排程任務

在設定長期排程前，先測試工具是否正常運作：

```bash
# 1. 手動執行工具測試
> 執行 get_stock_price 工具，參數 symbol=AAPL

# 2. 確認結果正確後再設定排程
> 設定每天早上 9 點執行這個工具
```

### 5. 定期檢查排程狀態

```bash
# 每週檢查一次
> 顯示所有排程任務

# 查看執行次數和最後執行時間
```

---

## 疑難排解

### Q: 排程沒有執行？

**檢查項目**:
1. 確認 Kuro 在運行（Discord Bot 或 CLI + Web 模式）
2. 檢查任務是否啟用：`schedule_list()`
3. 查看日誌：`~/.kuro/logs/assistant.log`
4. 確認 next_run 時間是否正確

### Q: 排程執行失敗？

**排查**:
```bash
# 1. 查看 audit log
kuro --audit-query --tool your_tool_name

# 2. 手動執行工具測試
> 執行 your_tool_name 工具

# 3. 檢查工具參數是否正確
```

### Q: 如何更改排程時間？

```bash
# 方法 1: 刪除後重新建立
schedule_remove(task_id="old-task")
schedule_add(...)  # 用新的時間

# 方法 2: 手動編輯 ~/.kuro/scheduler.json
# 修改 schedule_time 欄位
# 重啟 Kuro
```

### Q: 排程會在 Kuro 重啟後保留嗎？

是的！所有排程任務都儲存在 `~/.kuro/scheduler.json`，重啟後自動載入。

---

## 進階功能

### 串接多個工具

使用 Skill 指導 LLM 依序執行多個工具：

**Skill 範例**:
```markdown
---
name: morning-routine
description: Execute morning routine tasks
---

# Morning Routine

當執行 morning_routine 時，請依序執行：

1. 查詢今日天氣 (get_weather city="Taipei")
2. 查詢股票價格 (get_stock_price symbols=["AAPL", "TSLA"])
3. 抓取新聞摘要 (fetch_news category="tech", count=5)
4. 彙整結果並發送報告
```

**排程**:
```bash
schedule_add(
    task_id="morning-routine",
    name="Morning Routine",
    tool_name="execute_skill",  # 假設有這個工具
    parameters={"skill_name": "morning-routine"},
    schedule_type="daily",
    schedule_time="08:00"
)
```

---

## 安全性考量

### 排程任務的風險等級

排程工具本身是 MEDIUM 風險，因為它可以自動執行其他工具。

**建議**:
- 只排程你信任的工具
- 避免排程 HIGH/CRITICAL 風險等級的工具
- 定期檢查排程清單

### 在公用 Discord Bot 中

如果是公用 Bot，建議：

```yaml
# 限制誰可以新增排程
security:
  require_approval_for:
    - "schedule_add"
    - "schedule_remove"
```

或完全禁用：

```yaml
security:
  disabled_tools:
    - "schedule_add"
    - "schedule_remove"
```

只由管理員手動編輯 `scheduler.json`。

---

## 總結

Kuro 的排程系統讓您能夠：

✅ 自動化重複性任務
✅ 定時監控和通知
✅ 建立複雜的工作流程
✅ 24/7 持續運作

開始使用排程系統，讓 Kuro 成為您的自動化助手！🚀
