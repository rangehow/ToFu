---
name: agent-sft-rag-memory-landscape-2026
description: Agent SFT × RAG-Memory landscape (May 2026) — gap analysis for top-tier paper positioning
enabled: true
tags: [agents, sft, rag, literature, positioning]
created: 2026-05-24T00:26:08Z
updated: 2026-05-24T00:26:08Z
---

# Agent SFT × RAG-Memory: Landscape & Gap Analysis (May 2026)

Two parallel lines of work that rarely intersect, and one nascent bridge.

## Line A — Test-time trajectory memory (no weight update)
- ExpeL (2308.10144, AAAI'24) — insights from successful trajectories
- AWM (2409.07429, ICLR'25) — induced reusable workflows
- A-Mem (2502.12110) — note-linked agentic memory
- Mem0 / Letta / MemoryBank — conversational fact stores
- Dynamic Cheatsheet — cumulative/synthesized notes
- Trajectory-Informed Memory (2603.10600) — strategy/recovery/optimization tips at task & subtask level; +28.5pp AppWorld Diff-3
- ReasoningBank (ICLR'26 jL7fwchScm) — reasoning memories from successes & failures
- Memento 2 (2512.22716) — stateful reflective memory (theoretical)
- mem-agent (2601.23032) — RL-trained memory editor
- Evo-Memory benchmark (2511.20857) — Pearson ρ=0.55 between intra-task similarity and gain (memory helps only on structurally similar tasks)

## Line B — Agent SFT / parametric distillation
- FireAct (2310.05915, ICLR'24) — 500 GPT-4 traj → +77% tool-use
- AgentTuning / Lemur / xLAM / AgentGen — full-trajectory clone
- ATLAS (2503.02197) — train on 30% critical steps only beats 100%
- NAT (NAACL'25) — negative samples in SFT
- CLEANER (2601.15141) — self-purified trajectories for agentic RL
- TOUCAN (2510.01179) — 1.5M synthetic tool-agentic trajectories
- SynthAgent (2512.13564) — synthetic web-agent supervision
- OpenSeeker-v2 — 10.6k high-quality traj rivals 4-stage RL pipeline

## Bridge — closest prior art to a flywheel paper
- **"Fine-tuning with RAG"** (Ibrahim et al., ICLR'26, 2510.01375) — failure→hints→RAG-teacher→distill student. ALFWorld 79→91%, WebShop 61→72. ONE iteration; hints fixed at t=0; no quality scoring; success-only.
- "Self-Evolving Synthetic Data to Verifiable-Reward RL" (2601.22607) — RLVR side
- AWorld "Tune the Environment" (2510.10197) — environment design

## Gaps that survive scrutiny
- **G1 Granularity** — nobody has controlled-comparison of raw subtraj / step / tip / workflow / playbook under matched memory budget
- **G2 Iterative co-evolution** — Hint-Distill is 1-round; N-round flywheel convergence unknown
- **G3 Quality-aware joint training** — ATLAS critical-step + CLEANER purify + RAG-distill + NAT negatives have never been unified
- **G4 Failure-as-memory in parametric path** — failures only reach SFT loss as imitation-of-correction; antipattern unlikelihood losses unexplored

## Positioning options (for top-tier venue)
- **Option 1 (mechanistic, low risk):** "What Should Agents Remember? Controlled Study of Retrieval Granularity in Memory-Augmented Agent Fine-Tuning" — NeurIPS/ICLR
- **Option 2 (high reward, primary recommendation):** "Skill Compilation: Iteratively Distilling Retrieved Agent Memory into Parametric Skill" — N-iteration flywheel, granularity-aware, failure-aware, adaptive injection — directly extends Hint-Distill
- **Option 3 (artifact-driven):** "TrajectoryForge" — quality-aware retrieval-distillation pipeline + 1M traj release — D&B track

## De-risk checklist
1. Reproduce Hint-Distill ALFWorld 91% before claiming closed-loop novelty
2. Pick environments with limited overlap with prior art — AppWorld + ScienceWorld + SWE-bench
3. Define iteration kill-criterion (retrieval-ablation gap; skill-attention probe) before running iter 2
4. Failure-attribution module = hardest single piece; consider Trajectory-Informed Memory's 3-tip taxonomy or leaner antipattern/corrected-pattern pairs

