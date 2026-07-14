# Prompt 04 · 发布物料（抖音 + 小红书）

> 引用变量见 [prompts/00-variables.md](00-variables.md)。两平台都是 {ASPECT} 竖屏，但调性不同：
>
> - **抖音**：强钩子标题、蹭热点话题、引导完播；文案短。
> - **小红书**：封面文字是第一入口，正文走「教程 / 种草」口吻，话题标签要全，偏女性向。

## 任务

基于本期主题与 TOP{TOP_N} + 经典回归，生成两平台各自的发布物料。**必须包含**原作者署名占位 `{credits}`（署名 + 原链接），并在正文末尾追加固定尾部 `{FOOTER}`（接收投稿 / 北京随舞宣传报名 / 品牌合作 / 侵权联系删除，见 [00-variables](00-variables.md)），落实合规红线。

## 输出（严格 JSON）

```json
{
  "douyin": {
    "titles": ["3个候选，含1个数字/悬念钩子，≤20字"],
    "caption": "正文，末尾接 {credits} 署名与原链接，再接 {FOOTER} 固定尾部",
    "hashtags": ["#韩舞", "#舞蹈教程", "#新手跳舞", "#本周热舞", "..."],
    "pinned_comment": "置顶评论，引导互动的一句话",
    "trending_audio_note": "建议蹭的热门原声方向（注意 BGM 版权红线）"
  },
  "xiaohongshu": {
    "note_title": "笔记标题，可含 emoji，≤20字",
    "body": "正文：教程/种草口吻，分行，含每支星级与适合人群，末尾接 {credits} 再接 {FOOTER}",
    "cover_text": { "main": "封面主标（≤6字）", "sub": "副标（≤10字）" },
    "topics": ["#韩舞教程", "#新手舞蹈", "#跳舞日常", "#女团舞", "..."]
  }
}
```

## 输入

{EPISODE_THEME + PICKS + STARS + CLASSIC_COMEBACK}
