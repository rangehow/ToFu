# ChatUI Browser Extension - 安装与开发指南

## 🚀 快速开始

### 方法一：开发模式（推荐，最简单）

**Linux / macOS:**
```bash
./dev_extension.sh
```

**Windows:**
双击 `install_extension.bat`，选择选项 `1`

这将启动 Chrome 并自动加载扩展。修改代码后：
1. 访问 `chrome://extensions/`
2. 找到 ChatUI 扩展
3. 点击刷新按钮 ↻
4. 无需重启浏览器！

---

### 方法二：一键安装脚本

**Linux / macOS:**
```bash
./install_extension.sh
```

**Windows:**
双击 `install_extension.bat`

脚本提供三种选项：
1. **开发模式** - 直接加载源码目录（推荐）
2. **打包安装** - 打包为 ZIP 后安装
3. **仅打包** - 只生成 ZIP 文件

---

### 方法三：手动安装

1. 打开 Chrome 浏览器
2. 访问 `chrome://extensions/`
3. 开启右上角「开发者模式」
4. 选择以下任一方式：
   - **加载已解压的扩展程序** → 选择 `browser_extension/` 目录
   - **拖入 ZIP 文件** → 将打包好的 ZIP 拖入页面

---

## 📁 目录结构

```
chatui/
├── browser_extension/       # 浏览器扩展源码
│   ├── manifest.json        # 扩展配置
│   ├── background.js        # 后台脚本（核心逻辑）
│   ├── popup.html           # 弹出页面
│   ├── popup.js             # 弹出页逻辑
│   └── icon*.png            # 图标
├── lib/
│   ├── browser.py           # Python 端浏览器工具
│   └── tools.py             # 工具定义
├── dev_extension.sh         # 开发模式启动脚本
├── install_extension.sh     # 完整安装脚本（Linux/macOS）
└── install_extension.bat    # 完整安装脚本（Windows）
```

---

## 🛠️ 开发工作流

### 1. 启动开发环境
```bash
./dev_extension.sh
```

### 2. 修改代码
- 扩展逻辑：编辑 `browser_extension/background.js`
- Python 端：编辑 `lib/browser.py` 或 `lib/tools.py`

### 3. 测试扩展
- **扩展端修改** → `chrome://extensions/` → 点击刷新 ↻
- **Python 端修改** → 重启 ChatUI 服务器

### 4. 调试技巧

**查看扩展日志：**
1. `chrome://extensions/`
2. 找到 ChatUI 扩展
3. 点击「查看视图：background page」
4. 打开开发者工具 Console

---

## 🧪 新增工具（v2.0）

| 工具 | 用途 |
|------|------|
| `browser_summarize_page` | 快速获取页面摘要（框架、按钮、表单等） |
| `browser_get_app_state` | 提取应用状态（Vue/React/G6 数据） |
| `browser_get_interactive_elements` | 获取所有可交互元素 |
| `browser_click` | 点击指定元素 |
| `browser_screenshot` | 截图（支持自动压缩） |

---

## ⚠️ 常见问题

### Q: 修改代码后不生效？
A: 确保在 `chrome://extensions/` 点击了刷新按钮。

### Q: Chrome 提示「此扩展程序未打包」？
A: 开发模式正常提示，点击「取消」即可。

### Q: 脚本提示找不到 Chrome？
A: 编辑脚本，修改 `CHROME` 路径为你的实际安装位置。

---

## 📝 更新日志

### v2.0 (2025-03)
- ✨ 新增 `browser_summarize_page` 工具
- ✨ 新增 `browser_get_app_state` 工具
- ✨ 截图支持自动压缩
- ✨ Canvas 页面智能检测
- 🛠️ 新增一键安装脚本
