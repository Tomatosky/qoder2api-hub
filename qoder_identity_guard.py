"""qoder_identity_guard.py —— 出站身份句守卫（第三方 Agent 品牌特征最小中和）

背景（2026-11-23 实测定位）：Qoder 上游网关新增了对第三方 Agent 客户端
身份声明句的**字面子串检测**。命中即返回 HTTP 200 + SSE 内嵌
403/10605/RESOURCE_LIMIT 排队信封（queueType=p1、queueCount=0、
serviceAvailable=false、retryAfterSeconds=30）。该信封是**硬拒绝**而非
真实容量排队：等待 retryAfterSeconds 后重试仍被拒，交互式客户端
（ZCode / DSH / Claude Code / Codex 等）因此全面报错；官方 Qoder 客户端
与不含这些句子的请求（如 WorkBuddy 渠道）不受影响。

受控 A/B 实验（同账号、同出口、同模型，仅改 system 一句话）确认的触发
形态是**大小写敏感的字面匹配**，不是语义识别：

  模式 A —— 身份同位语（逗号必须紧跟品牌名）：
      "You are ZCode,"        命中
      "You are Claude Code,"  命中
      "You are Codex,"        命中
      "You are ZCode ,"       不命中（逗号前多一个空格）
      "You are ZCode."        不命中（无逗号）
      "You are zcode,"        不命中（大小写敏感）

  模式 B —— powered by 归属句（句号必须紧跟品牌名）：
      "powered by DeepSeek Harness."   命中
      "powered by DeepSeek Harness ."  不命中（句号前多一个空格）
      "powered by the DeepSeek Harness." 不命中

本模块的对策是**最小字符级中和**：只在命中片段内部插入一个空格
（逗号前 / 句号前），打断字面匹配。品牌名、语序、语义全部保留，
模型仍能看到完整的自我认知与工具指令；对不命中的文本零改动。

品牌匹配采用**通用正则**而非固定名单（上游随时可能增补品牌，名单
永远追不上）：品牌名 = 大写字母开头的词，允许空格/连字符连接的多词
（Claude Code / DeepSeek-Harness / GPT-5）。小写词（a / an / the /
my …）天然不匹配，因此 "You are a helpful assistant," 这类普通句子
零改动。未知品牌（如 XCode）同样被中和——多插一个空格语义无损，
宁可宽覆盖也不漏掉上游新增的检测项。

开关（默认关闭，显式开启才改写）：
  - 环境变量 QD_IDENTITY_GUARD=1/on/true/yes
  - 或 settings.json 的 "identity_guard": true（见 qoder_settings）

处理范围：出站请求体 messages 的全部文本 part（含 system / user /
assistant / tool 角色）。历史消息同样处理——多轮对话里上一轮的
身份句残留也会触发检测。
"""
import os
import re

# 通用品牌名形态：大写字母开头，词内允许字母数字，词间允许单个空格或
# 连字符（后续词须大写/数字开头，避免吞掉小写普通词）。
# 例：ZCode / Claude Code / DeepSeek Harness / DeepSeek-Harness / GPT-5
_BRAND = r"[A-Z][A-Za-z0-9]*(?:[ -][A-Z0-9][A-Za-z0-9]*)*"

# 与上游检测同口径：大小写敏感，逗号/句号必须**紧跟**品牌名。
# 中和后（标点前已有空格）不再命中 —— 天然幂等。
_IDENTITY_RE = re.compile(r"You are (" + _BRAND + r"),")
_POWERED_RE = re.compile(r"powered by (" + _BRAND + r")\.")


def guard_enabled(accounts_dir=None):
    """环境变量优先，其次 settings.json；默认关闭。

    accounts_dir 为 None 时只看环境变量（供无面板上下文的调用点）。
    """
    raw = str(os.environ.get("QD_IDENTITY_GUARD") or "").strip().lower()
    if raw in ("1", "on", "true", "yes", "enable", "enabled"):
        return True
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return False
    if accounts_dir is None:
        return False
    try:
        import qoder_settings
        return qoder_settings.identity_guard(accounts_dir)
    except Exception:
        return False


def neutralize_text(text):
    """对单个字符串做最小中和；返回 (新文本, 命中次数)。

    无命中时原样返回（同一对象），调用方可据此跳过复制。
    """
    if not isinstance(text, str) or not text:
        return text, 0
    hits = 0

    def _identity(m):
        nonlocal hits
        hits += 1
        # "You are ZCode," -> "You are ZCode ,"（逗号前插空格）
        return m.group(0)[:-1] + " ,"

    def _powered(m):
        nonlocal hits
        hits += 1
        # "powered by DeepSeek Harness." -> "powered by DeepSeek Harness ."
        return m.group(0)[:-1] + " ."

    out = _IDENTITY_RE.sub(_identity, text)
    out = _POWERED_RE.sub(_powered, out)
    return out, hits


def _neutralize_content(content):
    """content 可能是 str 或多模态 part 列表；只动 text part。"""
    if isinstance(content, str):
        return neutralize_text(content)
    if isinstance(content, list):
        hits = 0
        changed = False
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                new, n = neutralize_text(part["text"])
                if n:
                    part = dict(part, text=new)
                    hits += n
                    changed = True
            out.append(part)
        return (out if changed else content), hits
    return content, 0


def neutralize_messages(messages):
    """遍历出站 messages，中和全部文本中的身份句特征。

    返回 (新列表或原列表, 总命中次数)。无命中时返回原列表（零复制），
    保证开关关闭 / 无特征请求的路径与改动前逐字节一致。
    """
    total = 0
    changed = False
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        content = m.get("content")
        new_content, n = _neutralize_content(content)
        if n:
            item = dict(m, content=new_content)
            total += n
            changed = True
        # reasoning_content 是历史思考文本，同样可能残留身份句
        rc = item.get("reasoning_content")
        if isinstance(rc, str):
            new_rc, n2 = neutralize_text(rc)
            if n2:
                item = dict(item, reasoning_content=new_rc)
                total += n2
                changed = True
        out.append(item)
    return (out if changed else messages), total