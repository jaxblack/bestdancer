# BestDancer 标准化出片与发布流程

## 一条命令

```bash
source .venv/bin/activate
python3 scripts/auto_episode.py --publish
```

它依次执行：

1. **CDP/登录态预检**
   - 启动 `~/.chrome-debug-profile`，浏览器全程静音。
   - 下载前从浏览器实时导出各平台 cookies，不使用陈旧快照。
2. **跨平台发现**
   - 抖音 / TikTok / Instagram / YouTube 使用各自关键词和解析器。
   - 原始结果写入 `assets/incoming/<week>/candidates/`。
3. **采集效果 evaluation**
   - `pipeline/evaluate_discovery.py <week> --compare`
   - 评估采集量、元数据、时效、热度、可下载性、舞蹈匹配度、画面质量。
   - 历史写入 `output/discovery_eval/history.jsonl`，供关键词和筛选条件持续调优。
4. **下载**
   - 只下载候选池前列素材。
   - YouTube / Instagram / TikTok 下载前自动刷新登录 cookies。
   - 下载结果写 `assets/incoming/<week>/dl2/`。
5. **素材画面核对**
   - `scripts/verify_clips.py <week> --apply`
   - 用平台权威元数据校正作者和标题。
   - 抽帧判断是否真是舞蹈、真实舞种、人物数量和画面内容；非舞蹈淘汰。
6. **逐段 evaluation**
   - `pipeline/evaluate_segments.py <week> --apply`
   - 评估成片实际截取窗口的舞种、标题、初学者难度、表现力、竖版适配。
   - 表现力/竖版适配差的直接淘汰，由后续候选顺延顶替。
7. **版本化出片闭环**
   - render → evaluation → 执行 `repair_actions` → 再 render。
   - 可执行动作：换段、改标题/作者/舞种/难度、改起点/段长/亮度、压缩节奏、加强片头。
   - 每轮保留 `output/<week>_demo_vN.mp4`、config、manifest、report 和 frames。
   - 只有 `score >= threshold`、无 blocker、并且 `verdict=pass` 才结束。
8. **发布**
   - 上传脚本再次校验通过报告，闸门 fail-closed。
   - 成片超过 48MiB 时自动生成低码率 `_publish.mp4`，避免 Chrome 大文件注入崩溃。
   - 上传、填标题/简介、等待按钮可用、点击发布、确认跳转。
   - 回执写入 `output/publish/<week>.json`：平台审核中记为
     `submitted_reviewing`，审核通过并公开后才记为 `published`。
   - 已有提交/成功回执时拒绝重复发布。

## 音频标准

- 每段原片声先归一到 **-18 LUFS**，避免平台间忽大忽小。
- TTS 与原片声混入前目标差值约 **0–3dB**。
- 人声出现时使用温和 **3:1 sidechain ducking**，不再使用原先的 15:1 强压制。
- 最终成片目标 **-16 LUFS / true peak ≤ -1.5dBFS**。
- manifest 记录 `voice_active_lufs`、`bed_lufs`、`pre_duck_delta_db` 和 ducking ratio。
- `evaluate_demo.py` 把人声高于原片声 5dB 以上、低于 3dB 以上或 ducking 超过 6:1
  视为音频平衡问题。

## 版本与回退

- `output/<week>_demo.mp4`：当前最新版。
- `output/<week>_demo_vN.mp4`：不可变历史版本。
- `output/eval/<week>/report_vN.json`：该版本对应评估。
- 若新版本退步，从得分最高且问题最少的版本 config 分叉，不在差版本上继续叠改。
- 用户明确要求兜底时，只能发布历史上 `passed=true`、`verdict=pass` 的版本。

## 发布命令

```bash
# 全流程，及格后自动发布
python3 scripts/auto_episode.py --publish

# 发布指定通过版本
python3 scripts/upload_to_douyin.py \
  --week 2026-W31-C \
  --mp4 output/2026-W31-C_demo_v7.mp4 \
  --publish
```
