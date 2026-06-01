---
name: image-gen-chinese-text-limitation
description: generate_image 无法准确渲染中文字符，应生成纯背景让用户叠字
enabled: true
tags: [image-gen, chinese, poster, limitation]
created: 2026-05-05T07:02:37Z
updated: 2026-05-05T07:02:37Z
---

# generate_image 工具中文字符渲染缺陷

## 问题
`generate_image` (gemini-2.5-flash-image 后端) 无法准确渲染中文字符。即使在 prompt 中明确列出每个字符并要求"stroke-accurate typography"，输出仍会出现错字、乱码、笔画错误。

## 示例
要求渲染"东北大学 草坪音乐节"，实际输出是"朝北大学 蒂坪菬系节"之类的乱码。

## 推荐做法
当用户要求生成**带中文标题的海报/封面/banner**时：

1. **优先生成纯背景版本**：在 prompt 中强调 "NO TEXT, NO LETTERS, NO CHARACTERS — purely decorative background"，并**预留标题区域**（如顶部30%留白配金色filigree边框）。
2. **告诉用户在 PPT/PS/Figma 叠字**：推荐字体如思源宋体、方正兰亭、站酷高端黑。
3. **英文/数字可以渲染**：如 "2026 SUMMER"、"MUSIC FESTIVAL" 相对可靠。
4. **不要浪费 round 反复尝试让模型写对中文** — 直接说明限制并给出带空白的版本。

## 也适用于
- 日文汉字、繁体字、任何非拉丁字符标题
- 复杂英文排版（花体、艺术字）有时也不可靠

