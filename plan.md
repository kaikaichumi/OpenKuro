# 規劃：UI 操控電腦 (Computer Use via Desktop GUI Automation)

## 現狀分析

### 已有的功能
- `screenshot` 工具 — `mss` 截圖，存為 PNG
- `clipboard_read` / `clipboard_write` — 跨平台剪貼簿
- 瀏覽器自動化（Playwright）— `web_navigate`, `web_click`, `web_type`, `web_screenshot`, `web_close`
- Web GUI（FastAPI + WebSocket）— 聊天、審批、設定面板

### 缺少的功能
目前 Kuro 只能操控**瀏覽器內部**的元素，無法操控**桌面本身**。需要新增：
1. 滑鼠控制（移動、點擊、拖曳、滾輪）
2. 鍵盤控制（打字、快捷鍵、特殊按鍵）
3. 視覺迴圈（截圖 → 模型分析 → 動作 → 截圖 → ...）
4. Web GUI 即時呈現 AI 正在操作的畫面

---

## 實作計畫

### 步驟 1：新增桌面 GUI 自動化工具 (`src/tools/screen/desktop_control.py`)

新增依賴：`pyautogui`（開發計畫中已提及但未實作）

建立 3 個新工具：

**1-1. `mouse_action` 工具 (MEDIUM risk)**
```python
class MouseActionTool(BaseTool):
    name = "mouse_action"
    description = "控制滑鼠：移動、點擊、雙擊、右鍵、拖曳、滾輪"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["click", "double_click", "right_click", "move", "drag", "scroll"],
                "description": "滑鼠動作類型"
            },
            "x": {"type": "integer", "description": "目標 X 座標"},
            "y": {"type": "integer", "description": "目標 Y 座標"},
            "end_x": {"type": "integer", "description": "拖曳終點 X（僅 drag）"},
            "end_y": {"type": "integer", "description": "拖曳終點 Y（僅 drag）"},
            "scroll_amount": {"type": "integer", "description": "滾輪量（正=上, 負=下，僅 scroll）"}
        },
        "required": ["action", "x", "y"]
    }
    risk_level = RiskLevel.MEDIUM
```

**1-2. `keyboard_action` 工具 (MEDIUM risk)**
```python
class KeyboardActionTool(BaseTool):
    name = "keyboard_action"
    description = "控制鍵盤：打字、按鍵、快捷鍵"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["type", "press", "hotkey"],
                "description": "type=打字, press=按鍵, hotkey=快捷鍵"
            },
            "text": {"type": "string", "description": "要輸入的文字（僅 type）"},
            "key": {"type": "string", "description": "按鍵名稱（僅 press，如 enter, tab, escape）"},
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "快捷鍵組合（僅 hotkey，如 ['ctrl', 'c']）"
            }
        },
        "required": ["action"]
    }
    risk_level = RiskLevel.MEDIUM
```

**1-3. `screen_info` 工具 (LOW risk)**
```python
class ScreenInfoTool(BaseTool):
    name = "screen_info"
    description = "取得螢幕資訊：解析度、滑鼠位置"
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    risk_level = RiskLevel.LOW
```

**修改檔案：**
| 檔案 | 操作 |
|---|---|
| `pyproject.toml` | 新增 `pyautogui` 依賴 |
| `src/tools/screen/desktop_control.py` | **新建** — MouseActionTool, KeyboardActionTool, ScreenInfoTool |
| `src/tools/screen/__init__.py` | 更新 docstring |

---

### 步驟 2：支援截圖作為圖片傳給 Vision 模型

目前 `screenshot` 工具只回傳檔案路徑（純文字），LLM 看不到實際畫面。需要讓截圖結果能被 Vision 模型理解。

**2-1. 擴展 `ToolResult`，支援圖片附件**

在 `src/tools/base.py` 的 `ToolResult` 新增 `image` 欄位：
```python
@dataclass
class ToolResult:
    success: bool
    output: str = ""
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    image_path: str | None = None  # 新增：截圖路徑，引擎會自動轉為 vision content
```

**2-2. 修改 Engine 的工具結果處理**

在 `src/core/engine.py` 的 `process_message()` 中，當 `ToolResult.image_path` 不為 None 時，將工具回應訊息改為 multimodal content（包含 image_url）：

```python
# 如果工具結果包含截圖，構造 multimodal 訊息
if result.image_path:
    tool_msg = Message(
        role=Role.TOOL,
        content=[
            {"type": "text", "text": output},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{encode_image(result.image_path)}"
            }}
        ],
        name=tc.name,
        tool_call_id=tc.id,
    )
```

**2-3. 修改 `screenshot` 工具回傳 `image_path`**

在 `src/tools/screen/screenshot.py` 的回傳結果中加上 `image_path`：
```python
return ToolResult.ok(
    f"Screenshot saved: {filepath}\n...",
    image_path=str(filepath),  # 新增
    ...
)
```

**修改檔案：**
| 檔案 | 操作 |
|---|---|
| `src/tools/base.py` | `ToolResult` 新增 `image_path: str \| None = None` |
| `src/core/engine.py` | 工具結果處理支援 `image_path` → base64 圖片 |
| `src/core/types.py` | `Message.content` 支援 `list[dict]`（multimodal） |
| `src/tools/screen/screenshot.py` | 回傳時填入 `image_path` |

---

### 步驟 3：Computer Use 迴圈（截圖驅動的自動操作）

這是核心功能 — AI 看到螢幕截圖後，決定要執行什麼滑鼠/鍵盤操作，然後再截圖確認結果，循環直到完成任務。

**3-1. 新增 `computer_use` 複合工具 (`src/tools/screen/computer_use.py`)**

```python
class ComputerUseTool(BaseTool):
    name = "computer_use"
    description = (
        "啟動 computer use 模式：AI 會看到螢幕截圖，並透過滑鼠和鍵盤操控電腦完成指定任務。"
        "每一步都會自動截圖讓你確認目前畫面。"
        "建議搭配支援 Vision 的模型使用（如 Claude Sonnet, GPT-4o）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要完成的任務描述（如：打開記事本，輸入 Hello World）"
            }
        },
        "required": ["task"]
    }
    risk_level = RiskLevel.HIGH
```

此工具本身不直接執行操作，而是提示 AI 接下來需要透過 `screenshot` + `mouse_action` / `keyboard_action` 工具組合來完成任務。它的作用是返回初始截圖 + 提示訊息，引導 Agent Loop 進入 computer use 模式。

**實際上 Agent Loop 已經支援這個模式** — 只要 LLM 收到截圖後決定呼叫 `mouse_action` 或 `keyboard_action`，然後再呼叫 `screenshot` 確認結果，現有的 `process_message()` 迴圈就能自然處理。

`computer_use` 工具的 `execute()` 邏輯：
1. 自動截圖
2. 回傳截圖 + 提示文字：「以下是目前螢幕畫面。你的任務是：{task}。請使用 mouse_action / keyboard_action 工具來完成。每步操作後用 screenshot 確認結果。」

**修改檔案：**
| 檔案 | 操作 |
|---|---|
| `src/tools/screen/computer_use.py` | **新建** — ComputerUseTool |

---

### 步驟 4：Web GUI 即時螢幕預覽

讓使用者在 Web GUI 中看到 AI 正在操作的螢幕畫面。

**4-1. 新增 WebSocket 訊息類型**

Server → Client 新增：
```json
{
    "type": "screen_update",
    "image": "data:image/png;base64,...",
    "action": "click at (500, 300)",
    "step": 3
}
```

**4-2. 修改 `_handle_chat_message` 發送螢幕更新**

在 Engine 執行 `screenshot` / `mouse_action` / `keyboard_action` 時，透過 WebSocket 即時通知前端。

方式：在 `Engine` 新增可選的 `tool_callback`，每次工具執行完畢時呼叫。WebServer 實作此 callback，將截圖推送到前端。

```python
# Engine 新增
class ToolExecutionCallback:
    async def on_tool_executed(self, tool_name: str, params: dict, result: ToolResult) -> None:
        pass

# WebServer 實作
class WebToolCallback(ToolExecutionCallback):
    async def on_tool_executed(self, tool_name, params, result):
        if tool_name in ("screenshot", "computer_use") and result.image_path:
            await self._send_screen_update(result.image_path, tool_name, params)
```

**4-3. 前端新增螢幕預覽面板**

在 `index.html` 新增可摺疊的螢幕預覽區域：
```html
<div id="screen-preview" class="hidden">
    <div class="screen-header">
        <span>螢幕預覽</span>
        <span id="screen-step">步驟 0</span>
        <button class="panel-close" data-panel="screen-preview">×</button>
    </div>
    <div class="screen-content">
        <img id="screen-image" alt="Screen preview" />
        <div id="screen-action" class="screen-action-label"></div>
    </div>
</div>
```

在 `app.js` 處理 `screen_update` 訊息：
```javascript
case "screen_update":
    showScreenPreview(data);
    break;
```

在 `style.css` 新增螢幕預覽樣式（可伸縮、overlay 或 side panel）。

**修改檔案：**
| 檔案 | 操作 |
|---|---|
| `src/core/engine.py` | 新增 `ToolExecutionCallback` 介面 + 執行後 callback |
| `src/ui/web_server.py` | 實作 `WebToolCallback`，推送截圖到前端 |
| `src/ui/web/index.html` | 新增螢幕預覽 HTML |
| `src/ui/web/app.js` | 處理 `screen_update` 訊息 |
| `src/ui/web/style.css` | 螢幕預覽樣式 |

---

### 步驟 5：安全控制

**5-1. pyautogui 安全設定**

pyautogui 內建 FAILSAFE（滑鼠移到左上角自動停止），保留此功能。另加入：
- 操作間隔（`pyautogui.PAUSE = 0.3`）避免太快
- 座標邊界檢查（不允許操作超出螢幕範圍）

**5-2. 安全審批**

- `mouse_action` 和 `keyboard_action` 預設 MEDIUM risk
- `computer_use` 為 HIGH risk（因為是自動化循環）
- 建議將 `keyboard_action` 加入 `require_approval_for` 清單（可在 config.yaml 設定）

**5-3. 操作速率限制**

在 `desktop_control.py` 加入簡單的速率限制：
```python
_last_action_time: float = 0
MIN_ACTION_INTERVAL = 0.2  # 至少 200ms 間隔

async def execute(self, params, context):
    elapsed = time.monotonic() - self._last_action_time
    if elapsed < MIN_ACTION_INTERVAL:
        await asyncio.sleep(MIN_ACTION_INTERVAL - elapsed)
    # ... 執行動作
    self._last_action_time = time.monotonic()
```

**修改檔案：**
| 檔案 | 操作 |
|---|---|
| `src/tools/screen/desktop_control.py` | 加入安全限制（座標檢查、速率限制、FAILSAFE） |

---

## 完整修改檔案清單

| 檔案 | 操作 | 步驟 |
|---|---|---|
| `pyproject.toml` | 修改 — 新增 `pyautogui` | 1 |
| `src/tools/screen/desktop_control.py` | **新建** — MouseActionTool, KeyboardActionTool, ScreenInfoTool | 1, 5 |
| `src/tools/screen/__init__.py` | 修改 — 更新 docstring | 1 |
| `src/tools/base.py` | 修改 — ToolResult 新增 `image_path` | 2 |
| `src/core/engine.py` | 修改 — 支援 image_path、ToolExecutionCallback | 2, 4 |
| `src/core/types.py` | 修改 — Message.content 支援 multimodal | 2 |
| `src/tools/screen/screenshot.py` | 修改 — 回傳 image_path | 2 |
| `src/tools/screen/computer_use.py` | **新建** — ComputerUseTool | 3 |
| `src/ui/web_server.py` | 修改 — WebToolCallback、screen_update 推送 | 4 |
| `src/ui/web/index.html` | 修改 — 螢幕預覽面板 | 4 |
| `src/ui/web/app.js` | 修改 — 處理 screen_update | 4 |
| `src/ui/web/style.css` | 修改 — 螢幕預覽樣式 | 4 |

---

## 使用流程範例

### 透過聊天自然操作
```
使用者：幫我打開記事本，輸入 "Hello World" 然後存檔
AI：好的，我會操控你的電腦完成這個任務。讓我先看看目前的畫面。
    [呼叫 computer_use(task="打開記事本，輸入 Hello World 然後存檔")]
    [截圖 → 看到桌面]
    [呼叫 keyboard_action(action="hotkey", keys=["win", "r"])] → 開啟執行
    [截圖 → 看到執行對話框]
    [呼叫 keyboard_action(action="type", text="notepad")] → 輸入 notepad
    [呼叫 keyboard_action(action="press", key="enter")]
    [截圖 → 看到記事本開啟]
    [呼叫 keyboard_action(action="type", text="Hello World")]
    [呼叫 keyboard_action(action="hotkey", keys=["ctrl", "s"])]
    [截圖 → 看到存檔對話框]
    ...
AI：完成了！記事本已經打開並輸入了 "Hello World"，檔案已存檔。
```

### Web GUI 即時觀看
使用者在瀏覽器的 Web GUI 中，可以看到：
- 右側或下方的螢幕預覽面板
- 每一步操作的截圖即時更新
- 目前執行的動作描述（如 "click at (500, 300)"）
- 步驟計數器

---

## 技術注意事項

1. **模型要求** — computer use 需要支援 Vision 的模型（Claude Sonnet/Opus, GPT-4o 等）。使用 Ollama 本機模型時，需選用支援圖片的模型（如 llava）。
2. **pyautogui 限制** — Linux 需要 X11（不支援 Wayland 原生），macOS 需授予輔助使用權限。
3. **截圖解析度** — 全螢幕截圖可能很大，建議壓縮或縮放後再 base64 編碼傳給模型（降低 token 消耗）。
4. **現有 Agent Loop 相容** — 不需要另建循環，現有的 `process_message()` 多輪工具呼叫已能自然支援 computer use 模式。可能需要適當調高 `max_tool_rounds`。
