#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Agent LLM 客户端 - 使用 prompt_manager 配置的模型 API
"""

import json
import base64
import logging
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
                }
                logger.info(f"LLM 配置已加载: base_url={self._config['base_url']}, model={self._config['model_name']}")
        except Exception as e:
            logger.error(f"加载 LLM 配置失败: {e}")
            self._config = {}
        return self._config

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

        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": cfg.get("model_name", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        return self._call_api(url, body)

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

    def _call_api(self, url: str, body: dict, timeout: int = 300) -> str:
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
        }
        logger.info(f"LLM API 请求: {json.dumps(log_req, ensure_ascii=False)}")

        req = Request(url, data=data, headers=headers, method="POST")
        start_time = time.time()

        for attempt in range(3):
            try:
                logger.info(f"LLM API 调用中... (第{attempt+1}次尝试, timeout={timeout}s)")
                with urlopen(req, timeout=timeout) as resp:
                    elapsed = round(time.time() - start_time, 1)
                    resp_body = resp.read().decode()
                    result = json.loads(resp_body)
                    content = result["choices"][0]["message"]["content"]
                    content_len = len(content)

                    _log_api_call(log_req, {
                        "status": "success",
                        "attempt": attempt + 1,
                        "elapsed_s": elapsed,
                        "response_length": content_len,
                    })
                    logger.info(f"LLM API 成功 (耗时 {elapsed}s, 响应长度 {content_len})")
                    return content

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
                    # 重新构造 Request（因为之前的可能已被读取）
                    req = Request(url, data=data, headers=headers, method="POST")
                    raise RuntimeError(f"LLM API 调用失败 (已重试3次): {err_msg}")

                # 等待后重试
                time.sleep(2 * (attempt + 1))

        raise RuntimeError("LLM API 调用失败: 不可达的重试循环")


# 全局单例
_client: Optional[LLMClient] = None


def get_llm_client(prompt_manager_url: str = "http://localhost:8900") -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient(prompt_manager_url)
    return _client
