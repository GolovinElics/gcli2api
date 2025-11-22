"""
Message Optimizer - 优化消息历史以避免超出 token 限制
"""
import json
from typing import List, Dict, Any
from log import log


def estimate_tokens(text: str) -> int:
    """
    粗略估算文本的 token 数量
    中文：1个字符 ≈ 1.5 tokens
    英文：1个单词 ≈ 1.3 tokens
    """
    if not text:
        return 0
    
    # 统计中文字符
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    # 统计英文单词（粗略）
    english_words = len(text.split())
    
    # 估算
    return int(chinese_chars * 1.5 + english_words * 1.3)


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """估算单条消息的 token 数量"""
    tokens = 0
    
    # role 占用
    tokens += 4
    
    # content
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    tokens += estimate_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    # 图片大约占用 85-170 tokens
                    tokens += 128
    
    # tool_calls
    if "tool_calls" in message:
        tool_calls = message.get("tool_calls", [])
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                tokens += estimate_tokens(json.dumps(tc, ensure_ascii=False))
    
    return tokens


def optimize_messages(
    messages: List[Any],
    max_tokens: int = 120000,  # gpt-5-mini 的上下文限制
    reserve_tokens: int = 4000,  # 为响应预留的 tokens
) -> List[Any]:
    """
    优化消息列表，确保不超过 token 限制
    
    策略：
    1. 保留第一条消息（通常是 system prompt）
    2. 保留最后几条消息（最近的对话）
    3. 如果还是太长，压缩 system prompt
    """
    if not messages:
        return messages
    
    # 转换为字典格式以便处理
    msg_dicts = []
    for m in messages:
        if hasattr(m, "model_dump"):
            msg_dicts.append(m.model_dump())
        elif hasattr(m, "dict"):
            msg_dicts.append(m.dict())
        elif isinstance(m, dict):
            msg_dicts.append(m)
        else:
            msg_dicts.append({"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")})
    
    # 估算每条消息的 tokens
    token_counts = [estimate_message_tokens(m) for m in msg_dicts]
    total_tokens = sum(token_counts)
    
    log.debug(f"Message optimization - Total messages: {len(msg_dicts)}, Estimated tokens: {total_tokens}")
    
    # 如果在限制内，直接返回
    available_tokens = max_tokens - reserve_tokens
    if total_tokens <= available_tokens:
        log.debug("Messages within token limit, no optimization needed")
        return messages
    
    log.warning(f"Messages exceed token limit ({total_tokens} > {available_tokens}), optimizing...")
    
    # 策略1：保留 system prompt + 最近的消息
    optimized = []
    optimized_tokens = 0
    
    # 保留第一条消息（system prompt）
    if msg_dicts and msg_dicts[0].get("role") in ["system", "developer"]:
        system_msg = msg_dicts[0]
        system_tokens = token_counts[0]
        
        # 如果 system prompt 太长，压缩它
        if system_tokens > available_tokens * 0.3:  # 不超过 30%
            log.warning(f"System prompt too long ({system_tokens} tokens), compressing...")
            system_msg = _compress_system_prompt(system_msg, int(available_tokens * 0.3))
            system_tokens = estimate_message_tokens(system_msg)
        
        optimized.append(system_msg)
        optimized_tokens += system_tokens
        remaining_msgs = msg_dicts[1:]
        remaining_tokens = token_counts[1:]
    else:
        remaining_msgs = msg_dicts
        remaining_tokens = token_counts
    
    # 从后往前添加消息，直到达到限制
    for i in range(len(remaining_msgs) - 1, -1, -1):
        msg = remaining_msgs[i]
        tokens = remaining_tokens[i]
        
        if optimized_tokens + tokens <= available_tokens:
            optimized.insert(1 if optimized else 0, msg)  # 插入到 system 后面
            optimized_tokens += tokens
        else:
            # 如果是最后一条用户消息，必须保留（可能需要压缩）
            if i == len(remaining_msgs) - 1 and msg.get("role") == "user":
                log.warning(f"Last user message too long, compressing...")
                compressed = _compress_message(msg, available_tokens - optimized_tokens)
                optimized.insert(1 if optimized else 0, compressed)
                optimized_tokens += estimate_message_tokens(compressed)
            break
    
    log.info(f"Optimized messages: {len(msg_dicts)} -> {len(optimized)}, tokens: {total_tokens} -> {optimized_tokens}")
    
    # 转换回原始格式
    return _convert_back_to_original_format(optimized, messages)


def _compress_system_prompt(system_msg: Dict[str, Any], max_tokens: int) -> Dict[str, Any]:
    """压缩 system prompt"""
    content = system_msg.get("content", "")
    if not isinstance(content, str):
        return system_msg
    
    # 简单策略：保留开头和结尾，删除中间部分
    lines = content.split("\n")
    if len(lines) <= 10:
        # 太短，直接截断
        compressed = content[:int(max_tokens * 4)]  # 粗略估算
    else:
        # 保留前 30% 和后 30%
        keep_lines = int(len(lines) * 0.3)
        compressed_lines = lines[:keep_lines] + ["\n[... 中间内容已省略 ...]\n"] + lines[-keep_lines:]
        compressed = "\n".join(compressed_lines)
    
    return {**system_msg, "content": compressed}


def _compress_message(msg: Dict[str, Any], max_tokens: int) -> Dict[str, Any]:
    """压缩单条消息"""
    content = msg.get("content", "")
    if isinstance(content, str):
        # 简单截断
        max_chars = int(max_tokens * 4)  # 粗略估算
        if len(content) > max_chars:
            compressed = content[:max_chars] + "\n[... 内容已截断 ...]"
            return {**msg, "content": compressed}
    
    return msg


def _convert_back_to_original_format(optimized: List[Dict], original: List[Any]) -> List[Any]:
    """将优化后的字典列表转换回原始格式"""
    if not original:
        return optimized
    
    # 检查原始格式
    first_item = original[0]
    if isinstance(first_item, dict):
        return optimized
    
    # 如果是对象，尝试重建
    result = []
    for opt_dict in optimized:
        # 尝试使用原始类型
        try:
            if hasattr(first_item, "__class__"):
                obj = first_item.__class__(**opt_dict)
                result.append(obj)
            else:
                result.append(opt_dict)
        except Exception:
            result.append(opt_dict)
    
    return result if result else optimized
