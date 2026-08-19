#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Agent LLM 客户端 - 使用 prompt_manager 配置的模型 API
"""

import json
import base64
import logging
import re
import time
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger("agent_llm")

# API 调用日志目录 - 与主服务统一放在 backend/flow_data
API_LOG_DIR = os.environ.get(
    "LLM_API_LOG_DIR",
    os.path.join(os.path.dirname(__file__), "..", "backend", "flow_data", "api_logs")
)
os.makedirs(API_LOG_DIR, exist_ok=True)


def _split_thinking(content: str) -> tuple:
    """从回复中剥离思考内容，返回 (正文, 思考)

    需要处理三种模型输出形态（都在实测中出现过）：
      1. 标准闭合：  <think>思考</think>正文
      2. 只有闭合标签：思考</think>正文      ← 开标签被 chat template 吃掉
      3. 只有开标签：<think>思考（被 max_tokens 截断，无正文）

    形态 2 还常伴随"正文在 </think> 前后各出现一次"的重复，
    此时取后半段（真正的正文），避免把思考残留当正文返回。
    """
    if not content:
        return "", ""

    text = content.strip()
    reasoning = ""

    if "</think>" in text:
        head, _, tail = text.partition("</think>")
        reasoning = head.replace("<think>", "").strip()
        body = tail.strip()
        # 被 max_tokens 截断导致 </think> 后没有正文时，回退用前半段做正文
        if not body:
            body, reasoning = reasoning, ""
        text = body
    elif text.startswith("<think>"):
        # 只有开标签且未闭合：整段都是思考，没有可用正文
        return "", text[len("<think>"):].strip()

    # 清掉可能残留的孤立标签，避免裸标签泄漏到界面
    text = text.replace("<think>", "").replace("</think>", "").strip()
    return text, reasoning


def _dedup_reply(text: str) -> str:
    """去除模型把同一段回复输出两遍的情况

    实测 Qwen3 在关闭思考时会出现 "正文\n\n正文" 的整段重复。
    仅在两半完全一致（忽略空白）时才裁剪，避免误删正常的重复措辞。
    """
    if not text:
        return text
    stripped = text.strip()

    # 情况 1：以空行分隔的两段完全相同
    parts = [p.strip() for p in re.split(r"\n\s*\n", stripped) if p.strip()]
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]

    # 情况 2：整串正好是某段内容重复两次（可能只隔单个换行/空格）
    compact = re.sub(r"\s+", "", stripped)
    if len(compact) >= 8 and len(compact) % 2 == 0:
        half = len(compact) // 2
        if compact[:half] == compact[half:]:
            # 用原文按行还原前半段，保留原始格式
            lines = [l for l in stripped.split("\n") if l.strip()]
            if len(lines) % 2 == 0:
                mid = len(lines) // 2
                if re.sub(r"\s+", "", "".join(lines[:mid])) == compact[:half]:
                    return "\n".join(lines[:mid]).strip()
    return stripped


def clean_model_reply(content: str) -> tuple:
    """统一清洗模型原始输出，返回 (正文, 思考)

    provider=openai 与 provider=vllm 两条路径共用，避免只在一处处理导致行为不一致。
    """
    body, reasoning = _split_thinking(content)
    body = _dedup_reply(body)
    # 思考段与正文完全相同时说明并非真正的思考（模型把答案重复了一遍），不予保留
    if reasoning and re.sub(r"\s+", "", reasoning) == re.sub(r"\s+", "", body):
        reasoning = ""
    return body, reasoning


def _read_http_error(e: HTTPError) -> str:
    """读取 HTTPError 的响应体并提取可读错误信息

    urllib 的 HTTPError 默认只给出 "HTTP Error 400: Bad Request"，
    真正的原因在响应体里，必须显式读取，否则排查时完全看不到根因。
    """
    try:
        raw = e.read().decode("utf-8", errors="replace")
    except Exception:
        return getattr(e, "reason", "") or "无响应体"
    if not raw:
        return getattr(e, "reason", "") or "空响应体"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]
    # 兼容 OpenAI / vLLM / 各类网关的错误结构
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)[:500]
    if isinstance(err, str):
        return err[:500]
    return str(data.get("message") or data.get("detail") or data)[:500]


# 服务端未开启 function calling 的特征串（vLLM / 各类网关）
_TOOL_UNSUPPORTED_HINTS = (
    "enable-auto-tool-choice",
    "tool-call-parser",
    "tool choice",
    "tool_choice",
    "does not support tools",
    "tools is not supported",
    "function calling",
    "unsupported parameter: 'tools'",
)


def _is_tool_unsupported(status_code: int, detail: str) -> bool:
    """判断该错误是否由"服务端不支持工具调用"导致"""
    if status_code not in (400, 404, 422, 501):
        return False
    low = (detail or "").lower()
    return any(h in low for h in _TOOL_UNSUPPORTED_HINTS)


def _log_api_call(request_info: Dict[str, Any], response_info: Dict[str, Any]):
    """将 API 调用记录写入 jsonl 日志"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "service": "agent",
        **request_info,
        **response_info,
    }
    try:
        logfile = os.path.join(API_LOG_DIR, f"llm_api_{datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


class LLMClient:
    """调用 prompt_manager 系统中配置的 LLM API"""

    def __init__(self, prompt_manager_url: str = "http://localhost:8900"):
        self.pm_url = prompt_manager_url.rstrip("/")
        self._config: Optional[Dict[str, str]] = None

    def _load_config(self) -> Dict[str, str]:
        if self._config is not None:
            return self._config
        try:
            req = Request(f"{self.pm_url}/api/settings/llm/raw")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                self._config = {
                    "base_url": data.get("base_url", ""),
                    "api_key": data.get("api_key", ""),
                    "model_name": data.get("model_name", ""),
                    # provider 决定走哪种协议：openai(兼容接口) / vllm(本地原生接口)
                    "provider": (data.get("provider") or "openai").strip().lower(),
                    "version_name": data.get("version_name", ""),
                }
                logger.info(
                    f"LLM 配置已加载: provider={self._config['provider']}, "
                    f"base_url={self._config['base_url']}, model={self._config['model_name']}"
                )
        except Exception as e:
            logger.error(f"加载 LLM 配置失败: {e}")
            self._config = {}
        return self._config

    @property
    def provider(self) -> str:
        return (self._load_config().get("provider") or "openai").strip().lower()

    def is_vllm(self) -> bool:
        """当前激活配置是否为本地 vLLM 原生接口"""
        return self.provider == "vllm"

    def _refresh_config(self):
        self._config = None
        # 清空视觉能力缓存（避免切换模型后仍走旧的判定结果）
        try:
            from flow_parser_service import reset_vision_capability_cache
            reset_vision_capability_cache()
        except Exception:
            pass
        return self._load_config()

    def is_configured(self) -> bool:
        cfg = self._load_config()
        return bool(cfg.get("base_url") and cfg.get("model_name"))

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> str:
        """文本对话"""
        cfg = self._load_config()
        if not cfg.get("base_url"):
            raise RuntimeError("LLM API 未配置，请先在提示词管理系统中设置 Base URL")

        if self.is_vllm():
            msg = self._call_vllm(messages, temperature=temperature,
                                  max_tokens=max_tokens, **kwargs)
            return msg.get("content") or ""

        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": cfg.get("model_name", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        return self._call_api(url, body)

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 300,
        enable_thinking: Optional[bool] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """返回完整的 assistant message（含 tool_calls），供 ReAct 循环使用

        enable_thinking 为 None 时不下发任何思考相关参数（用服务端默认行为）。
        provider=vllm 时改走 service_vllm_llm.py 的 /llm/generate 协议。
        """
        cfg = self._load_config()
        if not cfg.get("base_url"):
            raise RuntimeError("LLM API 未配置，请先在提示词管理系统中设置 Base URL")

        if self.is_vllm():
            # 本地 vLLM 原生接口不支持 function calling，
            # 工具调用能力由上层 ReAct 循环在无 tool_calls 时自然降级
            return self._call_vllm(
                messages, temperature=temperature, max_tokens=max_tokens,
                timeout=timeout, enable_thinking=enable_thinking, tools=tools, **kwargs
            )

        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": cfg.get("model_name", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if enable_thinking is not None:
            body.update(self._thinking_params(cfg.get("model_name", ""), enable_thinking))

        return self._call_api(url, body, timeout=timeout, return_message=True)

    # ==================== 本地 vLLM 原生接口适配 ====================
    def _vllm_endpoint(self) -> str:
        """规整 vLLM 服务地址为 .../llm/generate

        允许用户在配置里填写以下任意形式：
            http://host:8608
            http://host:8608/
            http://host:8608/llm/generate
        """
        base = (self._load_config().get("base_url") or "").rstrip("/")
        if base.endswith("/llm/generate"):
            return base
        return base + "/llm/generate"

    def _call_vllm(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 300,
        enable_thinking: Optional[bool] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """调用 service_vllm_llm.py 部署的模型

        请求协议（对齐 VLLMHandler.on_post，单条模式）：
            {"messages": [{role, content}, ...], "temperature": ..,
             "top_p": .., "repetition_penalty": .., "max_new_tokens": .., "stop": [..]}
        响应协议：
            {"code": 200, "data": "生成的文本", "cost_time": .., "error": ""}
            注：单条请求时 data 是字符串；批量请求时 data 是字符串数组。
        返回值统一成 OpenAI 的 assistant message 结构，便于上层无感切换。
        """
        cfg = self._load_config()
        url = self._vllm_endpoint()

        # vLLM 服务端只接受纯 role/content，需剔除 tool_calls / tool 角色等 OpenAI 专有字段，
        # 否则 apply_chat_template 可能报错
        clean_messages = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""
            if role == "tool":
                # 工具结果降级为 user 观测信息，保留上下文而不破坏模板
                clean_messages.append({"role": "user", "content": f"[工具结果] {content}"})
            elif role in ("system", "user", "assistant") and content:
                clean_messages.append({"role": role, "content": content})

        body: Dict[str, Any] = {
            "messages": clean_messages,
            "temperature": temperature,
            # vLLM 服务端字段名是 max_new_tokens，不是 max_tokens
            "max_new_tokens": max_tokens,
        }
        for src, dst in (("top_p", "top_p"), ("repetition_penalty", "repetition_penalty"),
                         ("stop", "stop")):
            if kwargs.get(src) is not None:
                body[dst] = kwargs[src]

        if tools:
            logger.warning("provider=vllm 不支持 function calling，本轮已忽略 %d 个工具", len(tools))
        if enable_thinking:
            logger.info("provider=vllm 的思考模式由服务端 chat template 固定（enable_thinking=False）")

        log_req = {
            "url": url[:120],
            "provider": "vllm",
            "model": cfg.get("model_name", ""),
            "body_size_kb": round(len(json.dumps(body, ensure_ascii=False).encode("utf-8")) / 1024, 2),
            "timeout": timeout,
            "has_tools": bool(tools),
        }
        logger.info(f"vLLM 请求: {json.dumps(log_req, ensure_ascii=False)}")

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        start = time.time()
        last_err = None

        for attempt in range(3):
            try:
                req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode())
                elapsed = round(time.time() - start, 1)

                if result.get("code") != 200:
                    raise RuntimeError(f"vLLM 服务返回错误: {result.get('error') or result}")

                raw = result.get("data")
                # 单条请求返回 str；批量返回 list，这里统一取第一条
                content = (raw[0] if raw else "") if isinstance(raw, list) else (raw or "")
                raw_content = str(content)

                # 与 openai 分支共用同一套清洗逻辑（剥离思考 + 整段去重）
                content, reasoning = clean_model_reply(raw_content)

                _log_api_call(log_req, {
                    "status": "success",
                    "attempt": attempt + 1,
                    "elapsed_s": elapsed,
                    "cost_time": result.get("cost_time"),
                    "response_length": len(content),
                    "raw_length": len(raw_content),
                    "cleaned": len(raw_content) != len(content),
                })
                logger.info(f"vLLM 成功 (耗时 {elapsed}s, 响应长度 {len(content)})")

                msg: Dict[str, Any] = {"role": "assistant", "content": content}
                if reasoning:
                    msg["reasoning_content"] = reasoning
                return msg

            except Exception as e:
                last_err = e
                logger.warning(f"vLLM 调用失败 ({attempt + 1}/3): {e}")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

        _log_api_call(log_req, {
            "status": "error",
            "elapsed_s": round(time.time() - start, 1),
            "error": str(last_err),
        })
        raise RuntimeError(f"vLLM 服务调用失败: {last_err}")

    @staticmethod
    def _thinking_params(model_name: str, enable: bool) -> Dict[str, Any]:
        """按模型family生成思考开关参数

        各厂商协议不统一，这里按模型名前缀分派；未知模型同时下发几种常见写法，
        多余字段通常会被服务端忽略。

        重要：自建 vLLM 的 OpenAI 兼容接口**不认顶层 enable_thinking**（会被静默忽略），
        必须走 chat_template_kwargs 才能真正传给 tokenizer.apply_chat_template。
        因此这里统一附带 chat_template_kwargs，兼容自部署与云端。
        """
        m = (model_name or "").lower()

        # 传给 chat template 的参数（vLLM / SGLang 自部署的标准做法）
        tpl_kwargs = {"chat_template_kwargs": {"enable_thinking": enable}}

        # 阿里 Qwen3 / DashScope 兼容模式
        if "qwen" in m:
            return {"enable_thinking": enable, **tpl_kwargs}
        # 智谱 GLM-4.5+
        if "glm" in m:
            return {"thinking": {"type": "enabled" if enable else "disabled"}, **tpl_kwargs}
        # 字节豆包/火山方舟
        if "doubao" in m or "ep-" in m:
            return {"thinking": {"type": "enabled" if enable else "disabled"}}
        # DeepSeek：推理由模型版本决定（deepseek-reasoner），不支持运行时开关，
        # 但部分聚合网关接受 enable_thinking，因此仍下发
        if "deepseek" in m:
            return {"enable_thinking": enable}
        # Anthropic 风格
        if "claude" in m:
            return {"thinking": {"type": "enabled"}} if enable else {}

        return {
            "enable_thinking": enable,
            "thinking": {"type": "enabled" if enable else "disabled"},
            **tpl_kwargs,
        }

    def chat_with_image(
        self,
        prompt: str,
        image_path: str = None,
        image_base64: str = None,
        image_url: str = None,
        system_prompt: str = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 300,
    ) -> str:
        """
        多模态对话 - 支持图片输入
        支持：本地图片路径、base64 编码、URL
        """
        content_parts = [{"type": "text", "text": prompt}]

        if image_path:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            ext = image_path.rsplit(".", 1)[-1].lower()
            mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "gif": "image/gif", "webp": "image/webp"}
            mime = mime_map.get(ext, "image/png")
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{img_b64}"}
            })
            logger.info(f"多模态请求: image={image_path}, base64_size={len(img_b64)//1024}KB, mime={mime}")
        elif image_base64:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"}
            })
        elif image_url:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content_parts})

        cfg = self._load_config()
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": cfg.get("model_name", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        return self._call_api(url, body, timeout=timeout)

    def chat_with_images(
        self,
        prompt: str,
        image_paths: List[str] = None,
        system_prompt: str = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 300,
    ) -> str:
        """多图片多模态对话"""
        content_parts = [{"type": "text", "text": prompt}]

        if image_paths:
            for path in image_paths:
                with open(path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                ext = path.rsplit(".", 1)[-1].lower()
                mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                            "gif": "image/gif", "webp": "image/webp"}
                mime = mime_map.get(ext, "image/png")
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img_b64}"}
                })

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content_parts})

        cfg = self._load_config()
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": cfg.get("model_name", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        return self._call_api(url, body, timeout=timeout)

    def _call_api(self, url: str, body: dict, timeout: int = 300, return_message: bool = False):
        cfg = self._load_config()
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        # 计算请求体大小（用于日志，不记录完整内容）
        body_str = json.dumps(body, ensure_ascii=False)
        body_size = len(body_str.encode("utf-8"))
        data = body_str.encode("utf-8")

        log_req = {
            "url": url[:100],
            "model": cfg.get("model_name", ""),
            "body_size_kb": round(body_size / 1024, 2),
            "timeout": timeout,
            "has_image": "image_url" in body_str,
            "has_tools": bool(body.get("tools")),
        }
        logger.info(f"LLM API 请求: {json.dumps(log_req, ensure_ascii=False)}")

        req = Request(url, data=data, headers=headers, method="POST")
        start_time = time.time()
        tools_stripped = False

        for attempt in range(3):
            try:
                logger.info(f"LLM API 调用中... (第{attempt+1}次尝试, timeout={timeout}s)")
                with urlopen(req, timeout=timeout) as resp:
                    elapsed = round(time.time() - start_time, 1)
                    resp_body = resp.read().decode()
                    result = json.loads(resp_body)
                    message = result["choices"][0]["message"]
                    raw_content = message.get("content") or ""
                    # 统一清洗：剥离 <think> 残留 + 去重（原先只有 vllm 分支做了，
                    # openai 兼容分支漏掉，导致裸 </think> 和整段重复泄漏到界面）
                    content, reasoning = clean_model_reply(raw_content)
                    message["content"] = content
                    # 优先保留服务端返回的 reasoning 字段，缺失时用拆出来的
                    if reasoning and not (message.get("reasoning_content") or message.get("reasoning")):
                        message["reasoning_content"] = reasoning
                    content_len = len(content)

                    _log_api_call(log_req, {
                        "status": "success",
                        "attempt": attempt + 1,
                        "elapsed_s": elapsed,
                        "response_length": content_len,
                        "raw_length": len(raw_content),
                        "cleaned": len(raw_content) != len(content),
                        "tool_calls": len(message.get("tool_calls") or []),
                        "tools_stripped": tools_stripped,
                    })
                    logger.info(f"LLM API 成功 (耗时 {elapsed}s, 响应长度 {content_len})")
                    return message if return_message else content

            except HTTPError as e:
                # ① 读取服务端返回的错误体，否则只剩 "HTTP Error 400: Bad Request"，
                #    真实原因（如缺少 --enable-auto-tool-choice）会被完全掩盖
                elapsed = round(time.time() - start_time, 1)
                detail = _read_http_error(e)
                err_msg = f"HTTP {e.code}: {detail}"[:500]
                logger.warning(f"LLM API 调用失败 (第{attempt+1}次, 耗时 {elapsed}s): {err_msg}")

                # ② 服务端不支持 function calling 时，剥离 tools 自动降级重试
                if (not tools_stripped) and body.get("tools") and _is_tool_unsupported(e.code, detail):
                    logger.warning(
                        "服务端不支持工具调用，本轮自动降级为纯对话（如需工具能力，"
                        "请在 vLLM 启动参数中加 --enable-auto-tool-choice --tool-call-parser hermes）"
                    )
                    body = {k: v for k, v in body.items() if k not in ("tools", "tool_choice")}
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    log_req["has_tools"] = False
                    log_req["tools_stripped"] = True
                    tools_stripped = True
                    req = Request(url, data=data, headers=headers, method="POST")
                    continue  # 立即重试，不计入退避

                # 4xx 属于请求参数错误，重试无意义，直接失败（避免白等 8 秒）
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    _log_api_call(log_req, {
                        "status": "error", "attempt": attempt + 1,
                        "elapsed_s": elapsed, "error": err_msg,
                    })
                    raise RuntimeError(f"LLM API 请求被拒绝: {err_msg}")

                if attempt == 2:
                    _log_api_call(log_req, {
                        "status": "error", "attempt": attempt + 1,
                        "elapsed_s": elapsed, "error": err_msg,
                    })
                    raise RuntimeError(f"LLM API 调用失败 (已重试3次): {err_msg}")
                time.sleep(2 * (attempt + 1))
                req = Request(url, data=data, headers=headers, method="POST")

            except Exception as e:
                elapsed = round(time.time() - start_time, 1)
                err_msg = str(e)[:300]
                logger.warning(f"LLM API 调用失败 (第{attempt+1}次, 耗时 {elapsed}s): {err_msg}")

                if attempt == 2:  # 最后尝试失败
                    _log_api_call(log_req, {
                        "status": "error",
                        "attempt": attempt + 1,
                        "elapsed_s": elapsed,
                        "error": str(e),
                    })
                    raise RuntimeError(f"LLM API 调用失败 (已重试3次): {err_msg}")

                # 等待后重试（重新构造 Request，之前的 body 流可能已被读取）
                time.sleep(2 * (attempt + 1))
                req = Request(url, data=data, headers=headers, method="POST")

        raise RuntimeError("LLM API 调用失败: 不可达的重试循环")


# 全局单例
_client: Optional[LLMClient] = None


def get_llm_client(prompt_manager_url: str = "http://localhost:8900") -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient(prompt_manager_url)
    return _client
