# CardCraft — Markdown → 小红书卡片

将任意 Markdown 文章一键转为精美竖屏卡片图片，适合小红书、Instagram、Pinterest 等平台发布。

## ✨ 特性

- **100% 浏览器端运行** — 无需后端、无需安装，打开即用
- **智能分页** — 自动按章节拆分，像素级精确测量高度，绝不截断文字
- **6 款主题** — 紫罗兰、深海蓝、玫瑰粉、森林绿、暖阳橙、午夜黑
- **4 种比例** — 3:4（推荐）、2:3、9:16、1:1
- **代码高亮** — 自动语法着色（Python、JS、TS、Go 等）
- **精美表格** — 圆角阴影样式
- **一键下载** — 所有卡片打包为 ZIP
- **自定义署名** — 支持自定义品牌名
- **拖拽上传** — 直接拖入 .md 文件

## 🚀 使用方式

### 方式一：直接打开 HTML 文件
```bash
# 双击 index.html 即可在浏览器中打开
open index.html
```

### 方式二：静态服务器
```bash
cd tools/md2cards-web
python3 -m http.server 8080
# 访问 http://localhost:8080
```

### 方式三：部署到任何静态托管
将 `index.html` 上传到 GitHub Pages、Vercel、Netlify、Cloudflare Pages 等即可。
**只需一个文件**，零配置。

## 📐 技术架构

```
index.html (46KB, 单文件包含全部代码)
├── HTML — 编辑器 + 预览面板 + 设置栏
├── CSS  — 暗色 App Shell + 6 套卡片主题
└── JS   — Markdown 解析 + 智能分页 + 截图 + ZIP 打包

CDN 依赖（运行时加载）:
├── marked.js      — Markdown → HTML
├── highlight.js   — 代码语法高亮
├── html2canvas    — DOM → 截图
└── JSZip          — ZIP 打包下载
```

### 处理流程

```
Markdown 输入
    ↓
parseSections()     — 按 ## 标题拆分为章节
    ↓
preMergeSections()  — 将小章节合并（像素级测量是否能放在一张卡片内）
    ↓
adaptiveSplit()     — 超高章节拆分为多张卡片（按段落/代码块/表格等边界拆）
    ↓
buildCardHTML()     — 每张卡片生成带主题的 HTML
    ↓
html2canvas()       — DOM → PNG 截图
    ↓
JSZip              — 全部打包为 ZIP 一键下载
```

## 🎨 主题一览

| 主题 | 适合 | 风格 |
|------|------|------|
| 紫罗兰 Violet | 技术文章 | 优雅紫色渐变 |
| 深海蓝 Ocean | 商务/科技 | 清爽蓝色 |
| 玫瑰粉 Rose | 生活/时尚 | 温暖粉色 |
| 森林绿 Forest | 自然/健康 | 清新绿色 |
| 暖阳橙 Ember | 创业/激励 | 活力橙色 |
| 午夜黑 Midnight | 极客/暗黑 | 深色主题 |

## 💰 商业化方向

### 免费版（引流）
- 6 款主题
- 最多 15 张卡片
- 带 "Made with CardCraft" 水印

### 付费版（月订阅 ¥19.9/月）
- 不限主题
- 不限卡片数
- 无水印
- 自定义品牌 Logo
- 高清 2x 导出
- 批量处理 API

### 嵌入式 Widget（按调用收费）
```html
<!-- 第三方网站一行代码嵌入 -->
<script src="https://cardcraft.app/embed.js" data-key="YOUR_KEY"></script>
```

## 📝 License

MIT
