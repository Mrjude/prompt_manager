#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
流程树解析服务（统一实现，主页面与 Agent 页面共用）

设计原则：
1. LLM 输出**纯自然语言描述**（无 Markdown / JSON / 列表格式）
2. dialogue-flow-parser skill 仅作 OCR 辅助锚点
3. 不再产生 nodes / edges / parsed_flow 等结构化字段
"""

import os
import re
import sys
import json
import time
import shutil
import tempfile
import logging
import subprocess
from datetime import datetime
from typing import Tuple, List, Dict, Any, Optional
from pathlib import Path

from llm_client import get_llm_client

# ==================== 路径 ====================

SKILL_BASE = os.environ.get(
    "DIALOGUE_FLOW_PARSER_SKILL",
    "/data/songzb/.codebuddy/skills/dialogue-flow-parser",
)
SKILL_SCRIPT = os.path.join(SKILL_BASE, "scripts", "flow_parser.py")

FLOW_DATA_DIR = os.environ.get(
    "FLOW_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", "backend", "flow_data"),
)
os.makedirs(FLOW_DATA_DIR, exist_ok=True)

API_LOG_DIR = os.path.join(FLOW_DATA_DIR, "api_logs")
os.makedirs(API_LOG_DIR, exist_ok=True)


# ==================== 日志 ====================

_logger_initialized = False


def _setup_logger():
    global _logger_initialized
    if _logger_initialized:
        return
    plogger = logging.getLogger("flow_parser")
    plogger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log_file = os.path.join(
        API_LOG_DIR, f"flow_parser_{datetime.now().strftime('%Y%m%d')}.log"
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    plogger.addHandler(fh)
    root = logging.getLogger()
    if not root.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
        root.setLevel(logging.INFO)
    _logger_initialized = True


_setup_logger()
logger = logging.getLogger("flow_parser")


# ==================== 系统提示词（极简，抽取核心场景 + 分支逻辑） ====================

SYSTEM_PROMPT = """你是一个客服对话流程逻辑提炼助手。

输入可能是两种图片：
A. 流程树图 / 流程图（节点+连线+判断菱形+分支箭头）
B. 客服对话截图 / 聊天记录截图（访客与客服的多轮气泡对话）

请先自行判断类别，然后**只输出一段极简的自然语言**，用于概括这一个场景下的核心对话分支逻辑，不需要完整还原整张图或整段对话。

【输出目标】
- 只讲一件事：这个场景下访客大概想干什么、客服该怎么应对、遇到关键分支怎么走。
- 突出访客核心诉求（触发条件）+ 客服**避免什么/给出什么/如何过渡**的关键分支+ 主要的套联或收尾动作。
- 忽略寒暄、重复话术、时间戳、系统提示等噪音。

【输出风格】
- 一句话或者一小段（一般 60~180 字，最多不超过 250 字），自然口语。
- 允许使用"如果……就……" 等条件表达，但只写关键分支，不要穷举每一步。
- 禁止：Markdown、加粗、列表、编号、代码块、JSON、标题、前缀（如"流程描述："）、结尾解释。
- 城市名、门店名、姓名、手机号等隐私一律脱敏为"某城市"、"某门店"、"联系方式"等占位。

【风格示例（仅供参考，不要照抄）】
访客咨询门店地址时，客服不直接发详细地址，而是先表达在其所在地有院部，再以平台规则或预约制为由引导访客留下联系方式，由老师私发详细地址或定位；若访客反复追问，则重复解释规则并强调"避免白跑一趟"来推动留资。

现在按以上要求，直接输出这段极简描述本身。"""


def _build_user_prompt(ocr_text: str, page_index: int, total_pages: int) -> str:
    if total_pages > 1:
        head = (
            f"请分析这张图片（第 {page_index + 1}/{total_pages} 页）。"
            "自动判断它是流程树图还是客服对话截图，然后只用一小段自然口语，"
            "概括这个场景下的核心分支逻辑，突出访客诉求、客服关键动作和套联/收尾方式，不要展开描述每一步。"
        )
    else:
        head = (
            "请分析这张图片。自动判断它是流程树图还是客服对话截图，"
            "然后只用一小段自然口语（60~180 字，最多 250 字），"
            "概括这个场景下的核心分支逻辑，突出访客诉求、客服关键动作和套联/收尾方式，不要展开描述每一步。"
        )
    if ocr_text:
        head += (
            "\n\n以下是图片 OCR 得到的文字（按位置自上而下、自左而右排列），仅作辅助锚点，"
            "请以图片实际显示的内容为准：\n---\n" + ocr_text + "\n---"
        )
    return head


# ==================== 文件保存 / PDF ====================

def save_uploaded_file(flow_id: int, file_name: str, file_bytes: bytes) -> Tuple[str, str]:
    ext = Path(file_name).suffix.lower().lstrip(".")
    if ext == "pdf":
        file_type = "pdf"
    elif ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
        file_type = "image"
    else:
        file_type = "other"

    flow_dir = os.path.join(FLOW_DATA_DIR, str(flow_id))
    os.makedirs(flow_dir, exist_ok=True)
    safe_name = f"{int(time.time() * 1000)}_{file_name}"
    dest = os.path.join(flow_dir, safe_name)
    with open(dest, "wb") as f:
        f.write(file_bytes)
    logger.info(f"文件已保存: {dest} ({len(file_bytes)} bytes)")
    return dest, file_type


def pdf_to_images(pdf_path: str) -> List[str]:
    images: List[str] = []
    try:
        import fitz
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_p{i}.png", dir=tempfile.gettempdir())
            tmp.close()
            pix.save(tmp.name)
            images.append(tmp.name)
        doc.close()
        return images
    except ImportError:
        pass
    try:
        from pdf2image import convert_from_path
        for i, img in enumerate(convert_from_path(pdf_path, dpi=200)):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_p{i}.png", dir=tempfile.gettempdir())
            tmp.close()
            img.save(tmp.name, "PNG")
            images.append(tmp.name)
        return images
    except ImportError:
        raise RuntimeError("PDF 解析失败：请安装 PyMuPDF 或 pdf2image")


# ==================== Skill OCR ====================

_OCR_PY_CACHE: Optional[str] = None


def _get_ocr_python() -> Optional[str]:
    """返回一个已经装了 rapidocr_onnxruntime 的 Python 可执行文件路径。
    探测顺序：环境变量 OCR_PYTHON -> sys.executable -> 常见 miniconda/系统 python -> which python3。
    结果缓存到 _OCR_PY_CACHE，避免重复探测。
    """
    global _OCR_PY_CACHE
    if _OCR_PY_CACHE:
        return _OCR_PY_CACHE

    candidates: List[str] = []
    env_py = os.environ.get("OCR_PYTHON", "").strip()
    if env_py:
        candidates.append(env_py)
    candidates.append(sys.executable)
    for p in (
        "/data/miniconda3/bin/python3",
        "/data/miniconda3/bin/python",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ):
        if p not in candidates:
            candidates.append(p)
    for name in ("python3", "python"):
        which = shutil.which(name)
        if which and which not in candidates:
            candidates.append(which)

    for py in candidates:
        if not py or not os.path.isfile(py) or not os.access(py, os.X_OK):
            continue
        try:
            r = subprocess.run(
                [py, "-c", "import rapidocr_onnxruntime; print('ok')"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0 and "ok" in r.stdout:
                _OCR_PY_CACHE = py
                logger.info(f"选定 OCR Python: {py}")
                return py
        except Exception:
            continue
    logger.warning("未找到已安装 rapidocr_onnxruntime 的 Python 解释器；建议在服务运行环境中执行: pip install rapidocr_onnxruntime")
    return None


def run_skill_parser(image_path: str) -> Dict[str, Any]:
    if not os.path.exists(SKILL_SCRIPT):
        return {"status": "skipped"}
    ocr_py = _get_ocr_python()
    if not ocr_py:
        return {"status": "error", "message": "no python with rapidocr_onnxruntime found"}
    tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".json", dir=tempfile.gettempdir())
    tmp_out.close()
    try:
        result = subprocess.run(
            [ocr_py, SKILL_SCRIPT, "--input", image_path, "--output", tmp_out.name],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return {"status": "error", "message": (result.stderr or result.stdout)[:300]}
        with open(tmp_out.name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}
    finally:
        if os.path.exists(tmp_out.name):
            os.unlink(tmp_out.name)


_INLINE_OCR_SCRIPT = r"""
import sys, json
try:
    from rapidocr_onnxruntime import RapidOCR
except Exception as e:
    print("__NO_RAPIDOCR__:%s" % (str(e)[:200]))
    sys.exit(2)
engine = RapidOCR()
raw, _ = engine(sys.argv[1])
if not raw:
    print(json.dumps([]))
    sys.exit(0)
blocks = []
for item in raw:
    box, text, conf = item[0], item[1], item[2]
    xs = [p[0] for p in box]; ys = [p[1] for p in box]
    blocks.append({"text": text, "cy": sum(ys)/len(ys), "cx": sum(xs)/len(xs)})
print(json.dumps(blocks, ensure_ascii=False))
"""


def _run_inline_ocr(image_path: str) -> str:
    """内置 OCR 兜底：先尝试当前进程直接 import rapidocr；不行则子进程调用装了 rapidocr 的解释器。"""
    # 1) 当前进程直接跑
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        engine = RapidOCR()
        raw, _ = engine(image_path)
        if not raw:
            return ""
        blocks = []
        for item in raw:
            box, text, _conf = item[0], item[1], item[2]
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            blocks.append({"text": text, "cy": sum(ys)/len(ys), "cx": sum(xs)/len(xs)})
    except Exception:
        # 2) 子进程调用可用 python
        ocr_py = _get_ocr_python()
        if not ocr_py:
            return ""
        try:
            r = subprocess.run(
                [ocr_py, "-c", _INLINE_OCR_SCRIPT, image_path],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode != 0 or not r.stdout.strip() or r.stdout.startswith("__NO_RAPIDOCR__"):
                logger.warning(f"子进程 rapidocr 失败: {(r.stderr or r.stdout)[:200]}")
                return ""
            blocks = json.loads(r.stdout)
        except Exception as e:
            logger.warning(f"子进程 rapidocr 异常: {str(e)[:200]}")
            return ""
    if not blocks:
        return ""
    blocks.sort(key=lambda b: (b["cy"], b["cx"]))
    lines: List[List[Dict[str, Any]]] = []
    last_y = None
    for b in blocks:
        if last_y is None or abs(b["cy"] - last_y) > 15:
            lines.append([b])
        else:
            lines[-1].append(b)
        last_y = b["cy"]
    parts = []
    for line in lines:
        line.sort(key=lambda b: b["cx"])
        parts.append(" ".join(b["text"] for b in line))
    return "\n".join(parts)


def _extract_ocr_text(skill_data: Dict[str, Any]) -> str:
    if not skill_data:
        return ""
    if skill_data.get("status") in ("error", "skipped"):
        return ""
    text = skill_data.get("ocr_context", "") or ""
    if not text:
        text = (skill_data.get("ocr") or {}).get("full_text", "") or ""
    return text


def get_ocr_text(image_path: str) -> str:
    """先跑 skill；skill 失败或 OCR 为空时，用内置 rapidocr 兜底。"""
    skill_data = run_skill_parser(image_path)
    text = _extract_ocr_text(skill_data)
    if text.strip():
        return text
    # skill 失败 / OCR 引擎不可用 / 返回为空，用当前进程内置 rapidocr 直接跑
    return _run_inline_ocr(image_path)


# ==================== 自然语言清洗 ====================

_FORMAT_RULES = [
    (re.compile(r"```[\s\S]*?```"), ""),
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),
    (re.compile(r"(?<![\*\w])\*(?!\s)([^\*\n]+?)(?<!\s)\*(?!\*)"), r"\1"),
    (re.compile(r"^[\-\*\+•]\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*\d+[\.、)]\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*[│├└─]+\s*", re.MULTILINE), ""),
]


def _clean_to_natural(text: str) -> str:
    if not text:
        return ""
    s = text.strip()

    # 若 LLM 仍输出 JSON，尝试抽取 natural_description
    if s.startswith("{") or "```json" in s:
        try:
            inner = s
            m = re.search(r"```json\s*\n([\s\S]*?)\n\s*```", s)
            if m:
                inner = m.group(1)
            else:
                m2 = re.search(r"\{[\s\S]*\}", s)
                if m2:
                    inner = m2.group(0)
            obj = json.loads(inner)
            if isinstance(obj, dict):
                for key in ("natural_description", "description", "overview"):
                    v = obj.get(key)
                    if isinstance(v, str) and v.strip():
                        s = v.strip()
                        break
        except Exception:
            pass

    for pat, repl in _FORMAT_RULES:
        s = pat.sub(repl, s)

    s = re.sub(r"\n{2,}", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"^(流程描述|描述|输出|结果|自然语言描述)\s*[：:]\s*", "", s.strip())
    return s.strip()


# ==================== LLM 解析 ====================

_NO_IMAGE_PATTERNS = [
    "未提供图片", "没有提供图片", "未上传图片", "没有上传图片",
    "未收到图片", "没有收到图片", "未看到图片", "没有看到图片",
    "无法看到图片", "看不到图片", "无法查看图片", "没有查看到图片",
    "无法读取图片", "无法识别图片", "无法访问图片",
    "no image", "cannot see", "can't see", "unable to see", "did not receive an image",
]


def _looks_like_missing_image(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    for kw in _NO_IMAGE_PATTERNS:
        if kw in text or kw in lower:
            return True
    return False


# 视觉能力探测缓存: {(base_url, model_name): bool}
_VISION_CAPABILITY_CACHE: Dict[Tuple[str, str], bool] = {}
# 1x1 透明 PNG 的 base64（用于稳定探测）
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_PROBE_HINT = (
    "这是探测请求。请只回答“是”或“否”："
    "你是否在这条消息里收到了图片附件？收到就回答“是”，没收到就回答“否”。"
)


def _probe_vision_capability(llm) -> bool:
    """探测当前激活模型是否支持图片输入，按 (base_url, model_name) 缓存。"""
    cfg = llm._load_config() if hasattr(llm, "_load_config") else {}
    key = (cfg.get("base_url", ""), cfg.get("model_name", ""))
    if key in _VISION_CAPABILITY_CACHE:
        return _VISION_CAPABILITY_CACHE[key]
    try:
        raw = llm.chat_with_image(
            prompt=_PROBE_HINT,
            image_base64=_PROBE_PNG_B64,
            system_prompt="你是视觉能力探测器，严格按用户要求只回答“是”或“否”。",
            temperature=0.0,
            max_tokens=16,
            timeout=30,
        )
        text = (raw or "").strip()
        if not text or _looks_like_missing_image(text):
            supported = False
        else:
            head = text[:6].lower()
            if ("是" in text[:6]) or head.startswith(("yes", "y", "有", "看到")):
                supported = True
            elif ("否" in text[:6]) or head.startswith(("no", "n", "没")):
                supported = False
            else:
                supported = True
        _VISION_CAPABILITY_CACHE[key] = supported
        logger.info(
            f"视觉能力探测: 模型 {key[1]} => {'支持' if supported else '不支持'} 图片输入（返回:{text[:40]}）"
        )
        return supported
    except Exception as e:
        _VISION_CAPABILITY_CACHE[key] = False
        logger.info(f"视觉能力探测: 模型 {key[1]} 探测异常 => 判定为不支持图片：{str(e)[:120]}")
        return False


def reset_vision_capability_cache() -> None:
    """切换 LLM 配置后可清空视觉能力缓存。"""
    _VISION_CAPABILITY_CACHE.clear()


def _parse_from_ocr_text(ocr_text: str, page_index: int, total_pages: int) -> Dict[str, Any]:
    """在图片解析失败或模型不支持图片时，用 OCR 文本走纯文本 chat 抽象流程逻辑。"""
    llm = get_llm_client()
    if not ocr_text.strip():
        return {"status": "unparsed", "description": "", "raw": "",
                "error": "OCR 未提取到任何文字，无法进行文本回退解析"}

    if total_pages > 1:
        head = (
            f"这是从图片（第 {page_index + 1}/{total_pages} 页）OCR 得到的文字，"
            "文字按位置自上而下、自左而右排列，可能包含气泡时间戳和干扰字符。"
        )
    else:
        head = "这是从图片 OCR 得到的文字，按位置自上而下、自左而右排列，可能包含气泡时间戳和干扰字符。"

    user_prompt = (
        head
        + "\n请先判断这段文字来自流程树图还是客服对话截图，然后按系统提示词的要求，"
          "只输出一小段自然口语（60~180 字，最多 250 字），"
          "概括这个场景下的核心分支逻辑，突出访客诉求、客服关键动作和套联/收尾方式，"
          "不要展开描述每一步。真实姓名/手机号/身份证等隐私信息一律用占位替代。"
          "\n---OCR文字开始---\n" + ocr_text + "\n---OCR文字结束---"
    )
    try:
        raw = llm.chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=512,
        )
        cleaned = _clean_to_natural(raw)
        if not cleaned:
            return {"status": "unparsed", "description": "", "raw": raw,
                    "error": "文本回退解析返回内容为空"}
        return {"status": "success", "description": cleaned, "raw": raw, "error": ""}
    except Exception as e:
        msg = str(e)[:500]
        logger.error(f"OCR 文本回退解析失败: {msg}")
        return {"status": "error", "description": "", "raw": "", "error": _diagnose_error(msg)}


def parse_image_with_llm(
    image_path: str,
    page_index: int = 0,
    total_pages: int = 1,
    skill_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """返回 {status, description, raw, error}
    策略：先尝试多模态图片直读；若模型不支持图片输入（返回"未提供图片"）或调用异常，
    自动 fallback 到 OCR 文本 + 纯文本 chat 抽象流程逻辑。
    """
    llm = get_llm_client()
    if not llm.is_configured():
        return {
            "status": "skipped", "description": "", "raw": "",
            "error": "LLM API 未配置，请先在提示词管理系统中配置 Base URL 和模型名称",
        }

    ocr_text = _extract_ocr_text(skill_data or {})
    if not ocr_text.strip():
        # skill 未返回文字（缺依赖 / python 环境不同），当前进程兜底跑一次内置 rapidocr
        inline = _run_inline_ocr(image_path)
        if inline.strip():
            ocr_text = inline
    logger.info(
        f"LLM 解析: page={page_index + 1}/{total_pages}, image={os.path.basename(image_path)}, ocr_len={len(ocr_text)}"
    )

    # 优先判断当前 LLM 是否支持图片输入：不支持则直接走 OCR + 纯文本 chat
    if not _probe_vision_capability(llm):
        logger.info("当前模型不支持图片输入，跳过多模态请求，直接走 OCR 文本解析")
        fb = _parse_from_ocr_text(ocr_text, page_index, total_pages)
        if fb["status"] == "success":
            return fb
        err = fb.get("error") or "OCR 文本解析失败"
        if not ocr_text.strip():
            if not _get_ocr_python():
                err = (
                    "当前模型不支持图片输入，且服务环境未安装 OCR 依赖，无法回退。"
                    "请在服务运行环境执行：pip install rapidocr_onnxruntime，或切换到支持视觉输入的多模态模型（如 qwen-vl-max、gpt-4o）。"
                )
            else:
                err = "当前模型不支持图片输入，OCR 也未识别到文字；请确认图片清晰可辨，或切换支持视觉输入的模型"
        return {"status": "failed", "description": "", "raw": "", "error": err}

    user_prompt = _build_user_prompt(ocr_text, page_index, total_pages)

    try:
        raw = llm.chat_with_image(
            prompt=user_prompt,
            image_path=image_path,
            system_prompt=SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=512,
            timeout=300,
        )
        cleaned = _clean_to_natural(raw)
        if cleaned and not _looks_like_missing_image(cleaned):
            return {"status": "success", "description": cleaned, "raw": raw, "error": ""}
        # 视觉失败：模型没看到图（如 deepseek-v4-flash），fallback 到 OCR 文本
        if _looks_like_missing_image(cleaned) or not cleaned:
            logger.warning("视觉模型未识别到图片内容，回退到 OCR 文本解析")
            fb = _parse_from_ocr_text(ocr_text, page_index, total_pages)
            if fb["status"] == "success":
                return fb
            # 回退也失败：拼接更完整的错误提示
            if not ocr_text.strip():
                err = "模型未读取到图片，且 OCR 未提取到任何文字；请切换支持视觉输入的多模态模型，或确认图片清晰可辨"
            else:
                err = fb.get("error") or "文本回退解析失败"
            return {"status": "failed", "description": "", "raw": raw, "error": err}
        return {"status": "unparsed", "description": "", "raw": raw, "error": "LLM 返回内容为空或仅格式符"}
    except Exception as e:
        msg = str(e)[:500]
        logger.warning(f"LLM 多模态调用失败，尝试 OCR 文本回退: {msg}")
        # 多模态直接抛异常（如模型不支持 image_url），也尝试 OCR fallback
        fb = _parse_from_ocr_text(ocr_text, page_index, total_pages)
        if fb["status"] == "success":
            return fb
        logger.error(f"LLM 调用失败（含回退）: {msg}")
        return {"status": "error", "description": "", "raw": "", "error": _diagnose_error(msg)}


def _diagnose_error(err: str) -> str:
    el = err.lower()
    if any(k in el for k in ("multimodal", "vision", "image_url")):
        return "模型不支持图片输入，请配置支持多模态（视觉）的大模型，如 gpt-4o、qwen-vl、glm-4v 等"
    if "401" in err:
        return "API Key 认证失败，请检查 Key 是否正确"
    if "403" in err:
        return "API Key 无权限"
    if "404" in err:
        return "API 接口不存在，请检查 Base URL 是否正确"
    if "timeout" in el:
        return "API 调用超时"
    if "connection" in el or "url" in el:
        return "网络连接失败，请检查 Base URL"
    return f"LLM API 调用失败：{err}"


# ==================== 主入口 ====================

def parse_file(file_path: str, file_type: str, file_name: str) -> Tuple[str, str, str, str]:
    """
    返回 (description, structure_json, status, error)
    description 为纯自然语言；structure_json 为最小化的页面级元信息（仅含状态，不含结构化节点）。
    """
    tmp_to_clean: List[str] = []
    logger.info("=" * 50)
    logger.info(f"📋 开始解析: file={file_name}, type={file_type}")

    try:
        if file_type == "image":
            image_paths = [file_path]
        elif file_type == "pdf":
            image_paths = pdf_to_images(file_path)
            tmp_to_clean = list(image_paths)
        else:
            return (f"不支持的文件类型：{file_type}", "{}", "failed", f"unsupported: {file_type}")

        total = len(image_paths)
        page_descs: List[str] = []
        page_meta: List[Dict[str, Any]] = []
        success_count = 0
        last_error = ""

        for i, img in enumerate(image_paths):
            skill_data = run_skill_parser(img)
            llm_r = parse_image_with_llm(img, page_index=i, total_pages=total, skill_data=skill_data)

            if llm_r["status"] == "success":
                success_count += 1
                if total > 1:
                    page_descs.append(f"第{i + 1}页：{llm_r['description']}")
                else:
                    page_descs.append(llm_r["description"])
            else:
                err = llm_r.get("error") or "未知错误"
                last_error = err
                if llm_r["status"] == "skipped":
                    page_descs.append(err)
                else:
                    page_descs.append(f"第{i + 1}页解析失败：{err}" if total > 1 else f"解析失败：{err}")

            page_meta.append({
                "page_index": i,
                "status": llm_r["status"],
                "ocr_len": len(_extract_ocr_text(skill_data)),
                "error": llm_r.get("error", ""),
            })

        description = "\n\n".join(page_descs).strip()
        structure = json.dumps({"pages": page_meta, "total_pages": total}, ensure_ascii=False)

        if success_count == total:
            status = "success"
            error = ""
        elif success_count > 0:
            status = "partial"
            error = last_error
        else:
            status = "failed"
            error = last_error or "全部页面解析失败"

        logger.info(f"📋 解析完成: status={status}, success={success_count}/{total}")
        logger.info("=" * 50)
        return description, structure, status, error

    except Exception as e:
        logger.error(f"❌ parse_file 异常: {e}", exc_info=True)
        return ("", "{}", "failed", str(e))
    finally:
        for p in tmp_to_clean:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass


def delete_record_file(file_path: str) -> bool:
    try:
        if file_path and os.path.exists(file_path):
            os.unlink(file_path)
            logger.info(f"文件已删除: {file_path}")
            return True
    except Exception:
        return False
    return False
