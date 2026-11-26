#!/usr/bin/env python3
"""Summarize a Hugo post (text + local images) using Gemini 4 Neon."""

import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-4-neon")
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
POST_PATH_INPUT = os.environ.get("POST_PATH", "").strip()
OVERWRITE = os.environ.get("OVERWRITE", "false").strip().lower() in ("1", "true", "yes")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 8 * 1024 * 1024

SYSTEM_PROMPT = """你是一位擅长整理技术与课程笔记的编辑。请严格仿照「纸间 · Paper Margin」博客的升级版「深度整理笔记」模板，结合文章原有文字与全部配图/PPT 截图内容，为这篇文章撰写高密度、强直观性的结构化总结。

输出格式要求（严格遵守）：
1. 以一级标题 `# 深度整理笔记` 开头。
2. 紧跟一个单段引用块导读：`> **本节主线：** ...`，随后加一条 `---` 分割线。
3. 主体按逻辑划分为 3～5 个二级标题章节（`## 一、...`、`## 二、...` 等），每章下拆分为 2～3 个三级标题小节（`### 1. ...`、`### 2. ...`），各章节之间用 `---` 分割。
   - **观点型小标题**：二级和三级标题尽量写成带结论或反问的观点句（例如「为什么覆盖率高不等于程序正确」），避免干巴巴的名词罗列，让读者扫读目录即可串起全课逻辑。
   - **高密度不注水**：严禁像逐字稿那样过度切碎或同义反复，确保每节信息扎实精炼。
4. 视觉化与具象锚点（融入对应章节中，不要生硬堆砌）：
   - **ASCII 状态/架构图**：全篇结合内容加入 1～2 张极简 ASCII 流程图、状态转移图或架构分层图（放在纯文本代码块中，排版对齐、清晰易读）。
   - **核心概念横向对比表**：全篇至少提炼 1 张标准 Markdown 表格，用于辨析成对概念、方法取舍或工程阶段分工（如适用）。
   - **极简代码或公式锚点**：当原文或截图涉及算法、状态机、代码缺陷或工程约束时，配 3～5 行极简代码/示例或一行核心表达式帮助具象理解。
5. 结尾固定设置二级标题 `## 复习速记`：
   - 最上方先用纯文本代码块画一条贯穿全课的纵向「核心知识链」（串联因果与演进主线）；
   - 下方再以编号列表（`1.` 至 `6.` 或 `7.`）浓缩全篇核心结论，每条采用 `**核心概念/结论：**` 加粗前置句式。
6. 排版风格：善用 `→` 串联演化与因果逻辑；专业术语、代码概念或状态表达式用行内代码块（`` `...` ``）；核心概念或隐喻使用直角引号 `「」`。
7. 严格忠实于原文文字和图片内容，不编造原文与截图中未出现的事实、案例或数据。
8. 只输出从 `# 深度整理笔记` 到 `## 复习速记` 的纯 Markdown 内容，不要包裹在 ```markdown 代码块中，不要输出 frontmatter，不要输出 `# 原始笔记与截图`。"""

def resolve_post_file(raw_input: str) -> Path:
    cleaned = raw_input.strip().strip("/")
    candidates = [
        Path(cleaned),
        Path("content") / cleaned,
        Path("content/posts") / cleaned / "index.md",
        Path("content/posts") / f"{cleaned}.md",
    ]
    if cleaned.startswith("posts/"):
        candidates.insert(1, Path("content") / cleaned / "index.md")
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(f"Cannot locate post markdown file for POST_PATH={raw_input!r}")


def split_frontmatter(text: str) -> tuple[str, str]:
    for delim in ("---", "+++"):
        pattern = rf"\A({re.escape(delim)}[ \t]*\r?\n.*?\r?\n{re.escape(delim)}[ \t]*\r?\n)(.*)\Z"
        m = re.match(pattern, text, flags=re.DOTALL)
        if m:
            return m.group(1), m.group(2)
    return "", text


def collect_images(post_file: Path, body: str) -> list[Path]:
    post_dir = post_file.parent
    found: list[Path] = []
    seen: set[Path] = set()

    # 1. Images referenced in Markdown or HTML in order
    refs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", body)
    refs += re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', body, flags=re.IGNORECASE)
    for ref in refs:
        if ref.startswith(("http://", "https://", "data:")):
            continue
        p = (post_dir / ref.lstrip("/")).resolve()
        if not p.is_file() and ref.startswith("/"):
            p = (Path("static") / ref.lstrip("/")).resolve()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and p not in seen:
            seen.add(p)
            found.append(p)

    # 2. Fallback: any remaining images in the page bundle folder
    if post_file.name == "index.md":
        for p in sorted(post_dir.iterdir()):
            rp = p.resolve()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS and rp not in seen:
                seen.add(rp)
                found.append(p)

    return found[:MAX_IMAGES]


def call_gemini(body: str, images: list[Path]) -> str:
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in repository secrets.")

    parts: list[dict] = [
        {
            "text": (
                "以下是这篇博客文章的原始 Markdown 文字内容，随后附带文章中的图片/截图。"
                "请结合文字与图片生成完整的「深度整理笔记」：\n\n"
                + body.strip()
            )
        }
    ]

    for img_path in images:
        size = img_path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            print(f"Skipping oversized image {img_path.name} ({size} bytes)")
            continue
        mime, _ = mimetypes.guess_type(str(img_path))
        mime = mime or "image/png"
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_ID}:generateContent"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP {e.code}: {err_body}") from e

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"No candidates returned from Gemini API: {data}")

    out_parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in out_parts if "text" in p).strip()

    # Strip accidental outer markdown fence
    text = re.sub(r"\A```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*\Z", "", text).strip()
    if not text.startswith("# 深度整理笔记"):
        text = "# 深度整理笔记\n\n" + text
    return text


def main() -> int:
    post_file = resolve_post_file(POST_PATH_INPUT)
    print(f"Target post: {post_file}")

    raw_text = post_file.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(raw_text)

    if "# 深度整理笔记" in body:
        if not OVERWRITE:
            print("Post already contains '# 深度整理笔记' and OVERWRITE is false. Skipping.")
            return 0
        marker = "# 原始笔记与截图"
        if marker in body:
            body = body.split(marker, 1)[1].lstrip("\r\n")
        else:
            print("Warning: '# 原始笔记与截图' marker not found; summarizing full body.")

    images = collect_images(post_file, body)
    print(f"Loaded {len(images)} image(s) for multimodal summarization using {MODEL_ID}.")

    summary = call_gemini(body, images)

    original_section = body.lstrip("\r\n")
    if not original_section.startswith("# 原始笔记与截图"):
        original_section = f"# 原始笔记与截图\n\n{original_section}"

    new_content = f"{frontmatter}\n{summary}\n\n---\n\n{original_section}"
    post_file.write_text(new_content, encoding="utf-8")
    print(f"Successfully updated {post_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
