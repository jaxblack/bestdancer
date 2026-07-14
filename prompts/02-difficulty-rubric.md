# Prompt 02 · 难度评分 rubric（1–5 星）

> 引用变量见 [prompts/00-variables.md](00-variables.md)。基准受众：{AUDIENCE}。

> ⚠️ LLM 直接打星非常不稳定，必须按维度打分再折算。对每支舞（含经典回归）各跑一次。

## 打分维度与锚点

对 5 个维度各打 1–5 分（整数），严格参照锚点：

| 维度 | 权重 | 1 分（易） | 5 分（难） |
|---|---|---|---|
| `tempo` 速度 & 卡点密度 | 25% | 慢歌、动作稀疏 | 快歌高 BPM、密集卡点 |
| `complexity` 动作复杂度 | 25% | 简单手势 / 大动作 | isolation、body wave、复杂脚步 |
| `control` 协调 & 身体控制 | 20% | 单部位、易平衡 | 手脚分离、地板 / power move、柔韧 |
| `memory` 编舞长度 & 记忆负荷 | 15% | 高重复、几个八拍 | 段落多、几乎不重复 |
| `stamina` 体能强度 | 15% | 几乎不喘 | 连续跳跃 / 高强度持续 |

## 折算

```
weighted = 0.25*tempo + 0.25*complexity + 0.20*control + 0.15*memory + 0.15*stamina
stars    = round(weighted * 2) / 2   # 四舍五入到 0.5
```

星级含义：⭐ 入门 / ⭐⭐ 初级 / ⭐⭐⭐ 中级 / ⭐⭐⭐⭐ 中高级 / ⭐⭐⭐⭐⭐ 高级。

> 展示时：`stars` 内部保留 0.5 精度；封面 / 角标可把 5 个分项画成小雷达图，教学感更强。

## 输出（严格 JSON）

```json
{
  "scores": { "tempo": 0, "complexity": 0, "control": 0, "memory": 0, "stamina": 0 },
  "weighted": 0.0,
  "stars": 0.0,
  "fit": "适合人群（如：零基础可跟 / 有半年基础再上）",
  "hardest_part": "最难的一个点（10字内）"
}
```

## 输入

```
编舞：{TITLE} / 歌曲：{SONG} / 时长：{DURATION}
动作描述：{MOVE_NOTES}   # 由人工或视觉模型填写，评分只依据这里，不得脑补
```
