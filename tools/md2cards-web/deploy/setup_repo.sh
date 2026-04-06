#!/bin/bash
# ============================================================
# CardCraft — GitHub Pages Deployment Setup Script
# ============================================================
# Usage:
#   chmod +x setup_repo.sh
#   ./setup_repo.sh [YOUR_GITHUB_USERNAME]
#
# This script:
#   1. Creates a clean deployment directory
#   2. Initializes a git repo
#   3. Copies index.html + assets
#   4. Sets up GitHub Actions workflow
#   5. Gives you the commands to push
# ============================================================

set -e

USERNAME="${1:-YOUR_USERNAME}"
REPO_NAME="cardcraft"

# Resolve paths BEFORE any cd
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOY_DIR="$(pwd)/cardcraft-deploy"

echo ""
echo "✦ CardCraft — GitHub Pages Deployment"
echo "======================================"
echo ""

# --- Step 1: Create clean directory ---
if [ -d "$DEPLOY_DIR" ]; then
  echo "⚠️  $DEPLOY_DIR already exists. Remove it first or choose another location."
  exit 1
fi

mkdir -p "$DEPLOY_DIR"
cd "$DEPLOY_DIR"

echo "📁 Created $DEPLOY_DIR"

# --- Step 2: Copy files ---

# Copy the main HTML file
cp "$PARENT_DIR/index.html" ./index.html

# Copy manifest
cp "$SCRIPT_DIR/manifest.json" ./manifest.json

# Copy GitHub Actions workflow
mkdir -p .github/workflows
cp "$SCRIPT_DIR/.github/workflows/deploy.yml" .github/workflows/deploy.yml

# Create CNAME file (for custom domain, edit as needed)
# echo "cardcraft.app" > CNAME

# Create a robots.txt
cat > robots.txt << EOF
User-agent: *
Allow: /
Sitemap: https://${USERNAME}.github.io/${REPO_NAME}/sitemap.xml
EOF

# Create a simple sitemap
cat > sitemap.xml << EOF
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://${USERNAME}.github.io/${REPO_NAME}/</loc>
    <lastmod>$(date +%Y-%m-%d)</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
EOF

# Create 404.html (SPA redirect)
cat > 404.html << 'EOF'
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>CardCraft</title>
  <meta http-equiv="refresh" content="0; url=/">
</head>
<body>
  <p>Redirecting to <a href="/">CardCraft</a>...</p>
</body>
</html>
EOF

# Generate icons using Python (if available)
python3 - << 'PYEOF' 2>/dev/null || echo "⚠️  Python not found, skipping icon generation. Add icon-192.png and icon-512.png manually."
from PIL import Image, ImageDraw, ImageFont
import sys

for size in [192, 512]:
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Rounded rectangle background
    r = size // 5
    draw.rounded_rectangle([0, 0, size-1, size-1], radius=r, fill='#6C5CE7')
    # Star symbol
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size // 2)
    except:
        font = ImageFont.load_default()
    draw.text((size//2, size//2), "✦", fill='white', font=font, anchor='mm')
    img.save(f'icon-{size}.png')
    print(f'  ✅ Generated icon-{size}.png')
PYEOF

echo "📄 Files copied"
echo ""

# --- Step 3: Create README ---
cat > README.md << MDEOF
# ✦ CardCraft

**Markdown → 小红书卡片生成器**

免费在线工具，将任意 Markdown 文章一键转为精美竖屏卡片图片。

🔗 **在线使用** → [点击这里](https://${USERNAME}.github.io/${REPO_NAME}/)

## ✨ 特性

- 🎨 **6 款精美主题** — 紫罗兰、深海蓝、玫瑰粉、森林绿、暖阳橙、午夜黑
- 📐 **4 种比例** — 3:4（推荐）、2:3、9:16、1:1
- 🧠 **智能分页** — 像素级精确测量，绝不截断文字
- 💻 **代码高亮** — 自动语法着色
- 📦 **一键下载** — 所有卡片打包为 ZIP
- 🔒 **隐私安全** — 100% 浏览器端运行，无需上传

## 🚀 部署

本项目是一个纯静态单页应用（Single HTML File），可以部署到任何静态托管服务。

已通过 GitHub Actions 自动部署到 GitHub Pages。

## 📝 License

MIT
MDEOF

echo ""

# --- Step 4: Init git ---
git init
git add -A
git commit -m "🎉 Initial commit — CardCraft v1.0"

echo ""
echo "======================================"
echo "✅ 部署仓库准备就绪！"
echo "======================================"
echo ""
echo "接下来执行以下命令："
echo ""
echo "  1. 在 GitHub 上创建仓库: https://github.com/new"
echo "     仓库名: ${REPO_NAME}"
echo "     设置为 Public"
echo ""
echo "  2. 推送代码:"
echo "     cd ${DEPLOY_DIR}"
echo "     git remote add origin git@github.com:${USERNAME}/${REPO_NAME}.git"
echo "     git branch -M main"
echo "     git push -u origin main"
echo ""
echo "  3. 启用 GitHub Pages:"
echo "     Settings → Pages → Source → GitHub Actions"
echo ""
echo "  4. 等待 1-2 分钟，访问:"
echo "     https://${USERNAME}.github.io/${REPO_NAME}/"
echo ""
echo "  5. (可选) 自定义域名:"
echo "     Settings → Pages → Custom domain → 输入你的域名"
echo "     取消注释 CNAME 文件中的域名"
echo ""
