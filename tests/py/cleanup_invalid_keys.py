#!/usr/bin/env python3
"""
清理无效密钥数据脚本

此脚本会：
1. 删除 invalid_keys_ignored 配置项
2. 清理 StatsTracker 中非活跃密钥的统计数据
3. 清理日志文件中已删除密钥的记录（可选）

使用方法：
    python cleanup_invalid_keys.py [--clean-logs]
"""

import asyncio
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def cleanup():
    """清理无效密钥数据"""
    from src.storage_adapter import get_storage_adapter
    from src.key_manager import get_key_manager
    from src.stats_tracker import get_stats_tracker
    from log import log
    
    print("开始清理无效密钥数据...")
    
    # 1. 删除 invalid_keys_ignored 配置
    print("\n1. 删除 invalid_keys_ignored 配置...")
    adapter = await get_storage_adapter()
    try:
        await adapter.delete_config("invalid_keys_ignored")
        print("   ✓ 已删除 invalid_keys_ignored 配置")
    except Exception as e:
        print(f"   ✗ 删除失败: {e}")
    
    # 2. 清理 StatsTracker 中非活跃密钥的统计数据
    print("\n2. 清理非活跃密钥的统计数据...")
    try:
        key_manager = await get_key_manager()
        stats_tracker = await get_stats_tracker()
        
        # 获取当前所有密钥的索引
        all_keys = await key_manager.get_all_keys()
        active_indices = [key.index for key in all_keys]
        
        print(f"   当前有效密钥数量: {len(active_indices)}")
        
        # 清理非活跃密钥的统计数据
        await stats_tracker.cleanup_inactive_keys(active_indices)
        print("   ✓ 已清理非活跃密钥的统计数据")
    except Exception as e:
        print(f"   ✗ 清理失败: {e}")
    
    # 3. 清理日志文件（可选）
    if "--clean-logs" in sys.argv:
        print("\n3. 清理日志文件中已删除密钥的记录...")
        try:
            import re
            from log import log as logger
            
            log_file = logger.get_log_file()
            if not os.path.exists(log_file):
                print(f"   日志文件不存在: {log_file}")
                return
            
            # 读取当前配置的密钥
            cfg_keys = await adapter.get_config("assembly_api_keys", [])
            if isinstance(cfg_keys, str):
                cfg_keys = [k.strip() for k in cfg_keys.replace("\n", ",").split(",") if k.strip()]
            cfg_set = set(cfg_keys)
            
            print(f"   当前配置的密钥数量: {len(cfg_set)}")
            
            # 读取日志文件
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            print(f"   原始日志行数: {len(lines)}")
            
            # 过滤掉已删除密钥的记录
            pattern = re.compile(r"key=([^\s]+)")
            filtered_lines = []
            removed_count = 0
            
            for line in lines:
                match = pattern.search(line)
                if match:
                    key = match.group(1)
                    if key in cfg_set:
                        filtered_lines.append(line)
                    else:
                        removed_count += 1
                else:
                    # 没有 key 信息的行保留
                    filtered_lines.append(line)
            
            # 写回日志文件
            with open(log_file, "w", encoding="utf-8") as f:
                f.writelines(filtered_lines)
            
            print(f"   ✓ 已删除 {removed_count} 条记录")
            print(f"   保留日志行数: {len(filtered_lines)}")
        except Exception as e:
            print(f"   ✗ 清理日志失败: {e}")
    else:
        print("\n3. 跳过日志文件清理（使用 --clean-logs 参数启用）")
    
    print("\n清理完成！")
    print("\n提示：")
    print("  - 刷新控制面板页面以查看更新后的统计数据")
    print("  - 如果需要清理日志文件，请运行: python cleanup_invalid_keys.py --clean-logs")


if __name__ == "__main__":
    asyncio.run(cleanup())
