# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
# -*- coding: utf-8 -*-
"""Anthropic / OpenAI 协议一致性回归。

    python tests/_test_anthropic_conformance.py

覆盖两处此前审计出的偏差：
  1. /v1/messages 的 thinking 此前默认被忽略 —— 现在请求显式要求思考时
     按「空签名」输出（Key/面板/环境变量的显式 off 仍优先）；
  2. thinking block 此前排在正文之后 —— 现在按 Anthropic 协议排在 text 之前。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

_tmp_conv = tempfile.mkdtemp(prefix="qd-anth-conv-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_conv

P, A = bootstrap()
import inspect
import qoder_anthropic as AN

try:
    print("[1] wants_thinking：哪些请求算「显式要求思考」")
    check("enabled + budget_tokens", AN.wants_thinking(
        {"thinking": {"type": "enabled", "budget_tokens": 2048}}))
    check("adaptive（Claude Code 对第三方模型名）", AN.wants_thinking(
        {"thinking": {"type": "adaptive"}}))
    check("output_config.effort（无 thinking 块）", AN.wants_thinking(
        {"output_config": {"effort": "high"}}))
    check("disabled 不算", not AN.wants_thinking(
        {"thinking": {"type": "disabled"}}))
    check("完全没给不算", not AN.wants_thinking({}))
    check("空 body 不抛异常", AN.wants_thinking(None) is False)

    print()
    print("[2] anthropic_thinking_mode：请求驱动 + 显式 off 优先")
    _old_env = os.environ.pop("QD_ANTHROPIC_THINKING", None)
    try:
        check("无请求 thinking -> off（既有默认不回退）",
              P.anthropic_thinking_mode() == "off")
        check("请求 enabled -> empty",
              P.anthropic_thinking_mode(
                  None, {"thinking": {"type": "enabled",
                                      "budget_tokens": 2048}}) == "empty")
        check("请求 adaptive + effort -> empty",
              P.anthropic_thinking_mode(
                  None, {"thinking": {"type": "adaptive"},
                         "output_config": {"effort": "high"}}) == "empty")
        check("请求 disabled -> off",
              P.anthropic_thinking_mode(
                  None, {"thinking": {"type": "disabled"}}) == "off")
        check("Key 显式 off 压过请求 enabled",
              P.anthropic_thinking_mode(
                  {"anthropic_thinking": "off"},
                  {"thinking": {"type": "enabled",
                                "budget_tokens": 2048}}) == "off")
        check("Key 显式 empty 仍生效（无请求 thinking）",
              P.anthropic_thinking_mode(
                  {"anthropic_thinking": "empty"}) == "empty")
    finally:
        if _old_env is not None:
            os.environ["QD_ANTHROPIC_THINKING"] = _old_env
        else:
            os.environ.pop("QD_ANTHROPIC_THINKING", None)

    print()
    print("[3] 协议出口：请求驱动模式下真的产出 thinking block")
    _chat_obj = {"choices": [{"message": {
        "role": "assistant", "content": "answer",
        "reasoning_content": "thought"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    _mode = P.anthropic_thinking_mode(
        None, {"thinking": {"type": "enabled", "budget_tokens": 2048}})
    _res = AN.chat_to_messages(_chat_obj, model="m",
                               thinking_mode_override=_mode)
    _th = [b for b in _res["content"] if b.get("type") == "thinking"]
    check("响应含 thinking block", len(_th) == 1, _res["content"])
    check("thinking 文本来自 reasoning_content，签名为空",
          _th and _th[0]["thinking"] == "thought"
          and _th[0]["signature"] == "", _th)
    check("thinking 排在正文之前（Anthropic 协议顺序）",
          [b.get("type") for b in _res["content"]] == ["thinking", "text"],
          [b.get("type") for b in _res["content"]])
    _off = AN.chat_to_messages(
        _chat_obj, model="m",
        thinking_mode_override=P.anthropic_thinking_mode(
            None, {"thinking": {"type": "disabled"}}))
    check("disabled 请求不产出 thinking block",
          all(b.get("type") != "thinking" for b in _off["content"]))

    print()
    print("[4] 接线静态钉住：流式与非流式共用同一个 thinking 决策")
    _src = inspect.getsource(P.Handler._handle_anthropic)
    check("_handle_anthropic 用 payload 计算 thinking_mode",
          "anthropic_thinking_mode(" in _src and ", payload)" in _src)
    check("两条出口都传 thinking_mode_override=thinking_mode",
          _src.count("thinking_mode_override=thinking_mode") == 2,
          _src.count("thinking_mode_override=thinking_mode"))
finally:
    shutil.rmtree(_tmp_conv, ignore_errors=True)

finish()
