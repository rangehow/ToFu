# ICLR 高分论文特征研究（可附件交付包）

| 文件 | 内容 |
|---|---|
| `REPORT.md` | 研究报告：原分析核查、候选特征证据表、分数/录用/质量区分、可检验假设与防泄漏方案 |
| `analysis.py` | 分析脚本，仅依赖 numpy，固定随机种子 |
| `results.json` | 脚本对公开数据的全部数值输出（只含聚合统计，不含标题、作者、审稿文本） |
| `test_analysis.py` | 合成数据测试（无网络、无真实数据）：秩统计、ICC、Kruskal、workshop 过滤、确定性、无个人字段 |

本包**不含任何原始数据**。输入数据由使用者自行下载：papercopilot/paperlists 仓库未声明许可，原始文件也含作者字段，所以不随包分发。

## 复现

```bash
D=$(mktemp -d)
for y in 2023 2024; do curl -sL -o $D/iclr$y.json https://raw.githubusercontent.com/papercopilot/paperlists/main/iclr/iclr$y.json; done
curl -sL -o $D/iclr2026.json https://media.githubusercontent.com/media/papercopilot/paperlists/main/iclr/iclr2026.json
curl -sL -o $D/iclr2027_papers.json https://raw.githubusercontent.com/shiqiangw/iclr-2027-scores/main/docs/data/papers.json
sha256sum $D/*.json      # 与下表对照；上游更新后哈希会变，数值也会随之变化
python3 -m venv $D/v && $D/v/bin/pip install numpy pytest
$D/v/bin/python analysis.py --data-dir $D --iclr2027-papers $D/iclr2027_papers.json --out results.json
$D/v/bin/python -m pytest -q test_analysis.py
```

本次运行（2026-11-18）的输入哈希如下，脚本也会把它们写进 `results.json` 的 `inputs_sha256` 字段：

| 输入 | sha256 |
|---|---|
| iclr2023.json | `fac275dde32ef7b9ebeee8a8b38c0de41028b0f249cd8f2a2736cf25eb3c5120` |
| iclr2024.json | `88e6503079a92012b0aa2f84ecbb02c8ccbaebe61a7a7a0e707b30401894d6c8` |
| iclr2026.json | `3f5dd3a3b6279b8807cc2390974f2073fbb42cce7c52c8aa4a937c661a602165` |
| iclr2027_papers.json（2026-11-06T13:54Z 初审快照） | `888a41a3a24d4208ad338438168d431bf9d1e95f5f21c12cdebd0ac11054b05c` |

用这组输入连续运行两次，`results.json` 字节完全一致，sha256 为 `99dcb8b313877bce836fca3608d56f1e17f4c5864b509d913c19268102d5d18c`。
