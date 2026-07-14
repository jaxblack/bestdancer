# Prompt 05 · 片头 / 转场 / 片尾 / 经典回归卡（AI 视频生成）

> 引用变量见 [prompts/00-variables.md](00-variables.md)。全部 {ASPECT}，统一 {BRAND} 视觉。给 Kling / Runway / Pika 等文生视频工具用。

## 片头（约 5s）

```
neon "本周热舞 / WEEKLY DANCE" logo forming from light particles,
rhythmic beat-synced flashes, dynamic camera push-in,
vertical 9:16, high energy, clean typography space in center.
Negative: text artifacts, watermark, distorted letters.
```

## 转场（约 0.8s，可循环）

```
fast glitch / zoom whip-pan transition, motion blur, beat-drop feel,
seamless loopable, transparent-friendly, vertical 9:16.
```

## 经典回归卡（约 3s）

```
same neon theme, retro VHS tint overlay, big text space for "经典回归",
nostalgic glow, vertical 9:16.
```

用于从 TOP5 切到旧舞时做栏目分隔。

## 片尾（约 4s）

```
same neon theme, "关注追更下期" callout, subscribe motion, fade-out,
reserved space for 头像/二维码, vertical 9:16.
```

## 一致性要求

- 四段共用同一套配色 + 字体。季度更新只改 {BRAND} 变量。
- 预留安全区：顶部 / 右侧 / 底部各留约 15%，避免栏目名、头像被抖音 / 小红书 UI 遮挡。
