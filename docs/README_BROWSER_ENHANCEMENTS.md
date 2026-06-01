# 浏览器操作增强功能使用指南

本次更新参考了主流 Agent 框架 (Playwright, Selenium, BrowseAgent) 的最佳实践，大幅增强了浏览器操作能力，特别是针对**多步骤深层交互**场景。

---

## 🎯 新增功能总览

### 1. 基础操作增强（Extension 端）

| 命令 | 描述 | 参考框架 |
|------|------|----------|
| `hover_element` | 悬停元素，触发下拉菜单/工具提示 | Playwright `.hover()` |
| `keyboard_input` | 发送键盘输入，支持快捷键组合 | Playwright `.press()` |
| `wait_for_element` | 智能等待元素出现 | Selenium `WebDriverWait` |

### 2. 高级复合工具（Python 端）

| 工具 | 描述 | 使用场景 |
|------|------|----------|
| `browser_right_click_menu` | 右键点击并选择菜单项（支持子菜单） | 右键 → 选择 → 子菜单 |
| `browser_hover_and_click` | 悬停后点击菜单项 | 导航菜单、下拉列表 |
| `browser_fill_form` | 顺序填写表单并提交 | 多字段表单自动化 |

---

## 📦 安装与启用

### 1. 更新浏览器扩展

扩展代码已更新，需要重新加载：

1. 打开 `chrome://extensions/`
2. 找到 "ChatUI Browser Bridge"
3. 点击 **刷新** 按钮 🔄

或重新安装扩展：
```
GET /api/browser/download
```

### 2. Python 端

无需额外安装，新功能已集成到现有模块中：
- `lib/browser_advanced.py` - 高级工具实现
- `lib/tools.py` - 工具定义更新
- `lib/tasks.py` - 工具注册更新

---

## 💡 使用示例

### 示例 1: 右键菜单操作（你的核心场景）

**场景**: 右键点击元素 → 选择子菜单 → 点击任务列表

```python
from lib.browser_advanced import right_click_menu_select

# 简单右键菜单
result = right_click_menu_select(
    tab_id=123,
    target_selector="#task-element",
    menu_item_text="Actions",
    submenu_item_text="Task List",
    menu_wait=0.5,  # 等待菜单出现的时间
    timeout=5.0
)

if result['success']:
    print(f"✅ 完成，耗时 {result['elapsed_ms']}ms")
else:
    print(f"❌ 失败：{result['error']}")
    if 'available_items' in result:
        print(f"可找到的菜单项：{result['available_items']}")
```

**LLM 调用方式**:
```json
{
  "name": "browser_right_click_menu",
  "arguments": {
    "tabId": 123,
    "target_selector": "#task-element",
    "menu_item_text": "Actions",
    "submenu_item_text": "Task List"
  }
}
```

---

### 示例 2: 悬停触发下拉菜单

**场景**: 悬停导航菜单 → 点击下拉项

```python
from lib.browser_advanced import hover_and_click

result = hover_and_click(
    tab_id=123,
    hover_selector="nav .products-dropdown",
    click_selector="nav .dropdown-menu a[href='/pricing']",
    hover_wait=0.3
)
```

**LLM 调用方式**:
```json
{
  "name": "browser_hover_and_click",
  "arguments": {
    "tabId": 123,
    "hover_selector": "nav .products-dropdown",
    "click_selector": "nav .dropdown-menu a[href='/pricing']",
    "hover_wait": 0.3
  }
}
```

---

### 示例 3: 等待元素出现

**场景**: 等待动态加载的内容

```python
from lib.browser_advanced import wait_and_find_element

result = wait_and_find_element(
    tab_id=123,
    selector=".dynamic-content",
    condition="visible",  # 'present', 'visible', or 'clickable'
    timeout_ms=5000,
    poll_interval_ms=100
)

if result['found']:
    print(f"✅ 元素找到，等待了 {result['waited_ms']}ms")
```

**直接命令方式**:
```json
{
  "name": "browser_wait",
  "arguments": {
    "tabId": 123,
    "selector": ".dynamic-content",
    "condition": "visible",
    "timeout": 5000
  }
}
```

---

### 示例 4: 填写表单

**场景**: 顺序填写多字段表单

```python
from lib.browser_advanced import fill_form_sequential

result = fill_form_sequential(
    tab_id=123,
    fields=[
        {"selector": "input[name='name']", "value": "John Doe", "type": "type"},
        {"selector": "input[type='email']", "value": "john@example.com", "type": "type"},
        {"selector": "select[name='country']", "value": "United States", "type": "select"},
        {"selector": "input[name='agree']", "value": "", "type": "click"},
    ],
    submit_selector="button[type='submit']",
    field_delay=0.2
)
```

**LLM 调用方式**:
```json
{
  "name": "browser_fill_form",
  "arguments": {
    "tabId": 123,
    "fields": [
        {"selector": "input[name='name']", "value": "John Doe", "type": "type"},
        {"selector": "input[type='email']", "value": "john@example.com", "type": "type"}
    ],
    "submit_selector": "button[type='submit']",
    "field_delay": 0.2
  }
}
```

---

### 示例 5: 键盘快捷键

**场景**: 发送 Ctrl+S 保存，或 Escape 关闭菜单

```json
{
  "name": "browser_keyboard",
  "arguments": {
    "tabId": 123,
    "keys": "Ctrl+S"
  }
}
```

```json
{
  "name": "browser_keyboard",
  "arguments": {
    "tabId": 123,
    "keys": "Escape"
  }
}
```

**支持的快捷键**:
- 修饰键：`Ctrl`, `Alt`, `Shift`, `Meta` (Command)
- 特殊键：`Enter`, `Escape`, `Tab`, `Backspace`, `Delete`, `ArrowUp/Down/Left/Right`, `Home`, `End`, `PageUp`, `PageDown`, `F1-F12`
- 组合：`Ctrl+S`, `Ctrl+Shift+P`, `Alt+Tab`

---

## 🔧 最佳实践

### 1. 深层交互流程

对于复杂的多步骤交互，推荐流程：

```
1. browser_get_interactive_elements  → 获取页面元素列表
2. 分析元素，确定正确的选择器
3. browser_right_click_menu / browser_hover_and_click → 执行复合操作
4. browser_screenshot → 验证结果
5. 如有错误，根据 available_items 调整选择器重试
```

### 2. 等待策略

- 使用 `browser_wait` 替代 `time.sleep()` - 更可靠
- 设置合理的 `timeout`（默认 5 秒）
- 对于动画菜单，使用 `menu_wait=0.3~0.5` 秒

### 3. 错误处理

高级工具返回详细的错误信息：
```json
{
  "success": false,
  "steps_completed": 5,
  "error": "Menu item 'Task List' not found",
  "available_items": ["Actions", "Settings", "Help"]
}
```

利用 `available_items` 可以快速调试和修正选择器。

---

## 🎨 架构设计参考

### Playwright 风格
- **自动等待**: 操作前自动检查元素可见性
- **链式操作**: `hover().then(click())`
- **智能选择器**: 支持 text/css/xpath

### Selenium 风格
- **显式等待**: `WebDriverWait` 条件等待
- **隐式等待**: 全局超时设置
- **键盘支持**: `Keys.CONTROL + 's'`

### BrowseAgent 风格
- **工作流模板**: 预定义的交互模式
- **错误恢复**: 失败后自动重试/降级
- **元数据返回**: 详细的执行状态

---

## ⚠️ 注意事项

1. **浏览器原生右键菜单无法操作**
   - 扩展只能触发自定义右键菜单（网页 JS 实现的）
   - 浏览器原生菜单（打印、保存等）无法交互

2. **选择器稳定性**
   - 优先使用 `data-testid`、`id` 等稳定选择器
   - 避免使用动态生成的 class（如 `css-1a2b3c`）

3. **性能考虑**
   - 每次操作后会获取元素列表，复杂页面可能较慢
   - 使用 `viewport=true` 限制只获取可见区域元素

---

## 🚀 未来改进方向

1. **视觉定位**: 集成截图 + OCR，支持"点击屏幕坐标"
2. **智能重试**: 自动检测失败原因并重试
3. **录制回放**: 记录用户操作生成自动化脚本
4. **iframe 支持**: 跨 frame 元素操作
5. **Shadow DOM**: 支持 Web Components

---

## 📚 相关资源

- [Playwright 交互文档](https://playwright.dev/docs/input)
- [Selenium 等待策略](https://www.selenium.dev/documentation/webdriver/waits/)
- [BrowseAgent 工作流指南](https://docs.browseagent.pro/guides/workflows)
