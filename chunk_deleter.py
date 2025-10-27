import asyncio
import os
import shutil
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from plugin_manager import BotPlugin


class ChunkDeleterPlugin(BotPlugin):
    """区块备份、删除、还原插件"""
    
    name = "Chunk Deleter"
    version = "1.0.0"
    author = "MSMP_QQBot"
    description = "提供区块备份、删除、还原功能"
    
    # 插件配置
    DEFAULT_CONFIG = {
        "allowed_dimensions": ["overworld", "nether", "end"],  # 允许操作的维度
        "require_confirmation": True,  # 是否需要确认
        "backup_before_delete": True,  # 删除前是否备份
        "confirmation_timeout": 180  # 确认超时时间（秒）
    }
    
    # 服务端类型对应的世界文件夹结构
    SERVER_WORLD_STRUCTURES = {
        "plugin": {  # 插件服（如Paper）
            "overworld": "world",
            "nether": "world_nether/DIM-1", 
            "end": "world_the_end/DIM1"
        },
        "modded": {  # 模组服（如Fabric、Forge）
            "overworld": "world",
            "nether": "world/DIM-1",
            "end": "world/DIM1"
        },
        "vanilla": {  # 原版服
            "overworld": "world",
            "nether": "world/DIM-1",
            "end": "world/DIM1"
        }
    }
    
    def __init__(self, logger):
        super().__init__(logger)
        self.config = self.DEFAULT_CONFIG.copy()
        self.operation_history: List[Dict] = []
        self.user_cooldowns: Dict[int, float] = {}
        self.pending_confirmations: Dict[int, Dict] = {}
        self.server_working_directory = ""
        self.server_type = "unknown"
        self._confirmation_tasks: Dict[int, asyncio.Task] = {}
    
    async def on_load(self, plugin_manager) -> bool:
        """插件加载时的初始化"""
        try:
            self.plugin_manager = plugin_manager
            
            # 注册命令
            plugin_manager.register_command(
                command_name="delete_chunk",
                handler=self.handle_delete_chunk,
                names=["delete_chunk", "delchunk", "dc"],
                admin_only=True,
                description="删除指定坐标的区块文件和POI文件",
                usage="delete_chunk <x> <z> [维度] [coord_type] 或 delete_chunk confirm 确认删除",
                cooldown=10
            )
            
            plugin_manager.register_command(
                command_name="delete_chunk_area",
                handler=self.handle_delete_chunk_area,
                names=["delete_chunk_area", "delarea", "da"],
                admin_only=True,
                description="删除指定区域内的所有区块文件和POI文件",
                usage="delete_chunk_area <x1> <z1> <x2> <z2> [维度] [coord_type]",
                cooldown=30
            )
            
            plugin_manager.register_command(
                command_name="restore_chunk",
                handler=self.handle_restore_chunk,
                names=["restore_chunk", "restore", "rc"],
                admin_only=True,
                description="从备份还原区块文件和POI文件",
                usage="restore_chunk <x> <z> [维度] [coord_type]",
                cooldown=10
            )
            
            plugin_manager.register_command(
                command_name="backup_chunk",
                handler=self.handle_backup_chunk,
                names=["backup_chunk", "backup", "bc"],
                admin_only=True,
                description="手动备份区块文件和POI文件",
                usage="backup_chunk <x> <z> [维度] [coord_type]",
                cooldown=10
            )
            
            # 注册事件监听
            plugin_manager.register_event_listener(
                "server_started",
                self.on_server_started
            )
            
            plugin_manager.register_event_listener(
                "server_stopping", 
                self.on_server_stopping
            )
            
            self.logger.info(f"{self.name} v{self.version} 已加载")
            return True
            
        except Exception as e:
            self.logger.error(f"加载区块删除插件失败: {e}", exc_info=True)
            return False
    
    async def on_unload(self) -> None:
        """插件卸载时的清理"""
        try:
            # 取消所有待确认任务
            for task in self._confirmation_tasks.values():
                task.cancel()
            self._confirmation_tasks.clear()
            
            self.pending_confirmations.clear()
            self.logger.info(f"{self.name} 已卸载")
        except Exception as e:
            self.logger.error(f"卸载插件时出错: {e}", exc_info=True)
    
    async def on_config_reload(self, old_config: dict, new_config: dict) -> None:
        """配置重新加载时的处理"""
        for key in new_config:
            if old_config.get(key) != new_config.get(key):
                self.logger.info(f"区块删除插件配置已变更: {key}")
        
        # 更新配置
        self.config.update(new_config.get("chunk_deleter", {}))
    
    def _parse_coordinates(self, x_str: str, z_str: str, coord_type: str = "chunk") -> Tuple[int, int]:
        """解析坐标，支持区块坐标和世界坐标"""
        try:
            if coord_type == "world":
                # 世界坐标转区块坐标
                x_world = float(x_str)
                z_world = float(z_str)
                x_chunk = int(x_world // 16)
                z_chunk = int(z_world // 16)
                self.logger.info(f"世界坐标 ({x_world}, {z_world}) 转换为区块坐标 ({x_chunk}, {z_chunk})")
                return x_chunk, z_chunk
            else:
                # 区块坐标
                return int(x_str), int(z_str)
        except ValueError:
            raise ValueError(f"坐标格式错误: x={x_str}, z={z_str}, 类型={coord_type}")
    
    def _get_coord_type_from_parts(self, parts: List[str]) -> str:
        """从参数列表中获取坐标类型"""
        if len(parts) >= 4:
            last_arg = parts[3].lower()
            if last_arg in ["world", "block", "coord"]:
                return "world"
        if len(parts) >= 5:
            last_arg = parts[4].lower()
            if last_arg in ["world", "block", "coord"]:
                return "world"
        return "chunk"
    
    async def handle_delete_chunk(self, user_id: int, group_id: int, command_text: str, 
                                 config_manager=None, **kwargs) -> Optional[str]:
        """处理单个区块删除命令"""
        try:
            # 检查服务器目录
            if not await self._ensure_server_detected(config_manager):
                return "请先配置服务器工作目录"
            
            # 解析命令参数
            parts = command_text.strip().split()
            
            # 处理确认操作
            if parts and parts[0].lower() == "confirm":
                return await self._confirm_operation(user_id, group_id)
            
            # 解析坐标和维度
            if len(parts) < 2:
                return "用法: delete_chunk <x> <z> [维度] [coord_type]\n维度可选: overworld, nether, end (默认: overworld)\n坐标类型: chunk(区块坐标,默认), world(世界坐标)"
            
            try:
                # 获取坐标类型
                coord_type = self._get_coord_type_from_parts(parts)
                
                # 解析坐标
                x, z = self._parse_coordinates(parts[0], parts[1], coord_type)
                
                # 解析维度
                dimension = "overworld"
                if len(parts) > 2:
                    # 检查第三个参数是否是维度
                    if parts[2].lower() in self.config["allowed_dimensions"]:
                        dimension = parts[2].lower()
                    elif parts[2].lower() in ["world", "block", "coord"]:
                        coord_type = "world"
                
                # 如果还有第四个参数，检查是否是坐标类型
                if len(parts) > 3 and parts[3].lower() in ["world", "block", "coord"]:
                    coord_type = "world"
                    
            except ValueError as e:
                return f"错误: {str(e)}"
            
            # 验证维度
            if dimension not in self.config["allowed_dimensions"]:
                return f"错误: 不支持的维度 '{dimension}'，支持的维度: {', '.join(self.config['allowed_dimensions'])}"
            
            # 检查世界文件夹是否存在
            world_path = self._get_world_path(dimension)
            if not world_path or not os.path.exists(world_path):
                return f"错误: 找不到 {dimension} 维度的世界文件夹"
            
            # 检查区块文件或POI文件是否存在
            chunk_file = self._get_chunk_file_path(world_path, x, z)
            poi_file = self._get_poi_file_path(world_path, x, z)
            
            if not os.path.exists(chunk_file) and not os.path.exists(poi_file):
                coord_display = f"世界坐标({x*16}, {z*16})" if coord_type == "world" else f"区块坐标({x}, {z})"
                return f"错误: 区块文件和POI文件都不存在 - {dimension} {coord_display}"
            
            # 如果需要确认
            if self.config["require_confirmation"]:
                return await self._request_single_confirmation(user_id, x, z, dimension, coord_type)
            
            # 直接执行删除
            return await self._execute_chunk_deletion(user_id, x, z, dimension, "single", coord_type)
            
        except Exception as e:
            self.logger.error(f"处理区块删除命令失败: {e}", exc_info=True)
            return f"命令执行失败: {str(e)}"
    
    async def handle_delete_chunk_area(self, user_id: int, group_id: int, command_text: str,
                                      config_manager=None, **kwargs) -> Optional[str]:
        """处理区域区块删除命令"""
        try:
            # 检查服务器目录
            if not await self._ensure_server_detected(config_manager):
                return "请先配置服务器工作目录"
            
            # 解析命令参数
            parts = command_text.strip().split()
            
            if len(parts) < 4:
                return "用法: delete_chunk_area <x1> <z1> <x2> <z2> [维度] [coord_type]\n删除从(x1,z1)到(x2,z2)矩形区域内的所有区块文件和POI文件\n坐标类型: chunk(区块坐标,默认), world(世界坐标)"
            
            try:
                # 获取坐标类型
                coord_type = self._get_coord_type_from_parts(parts)
                
                # 解析坐标
                x1, z1 = self._parse_coordinates(parts[0], parts[1], coord_type)
                x2, z2 = self._parse_coordinates(parts[2], parts[3], coord_type)
                
                # 解析维度
                dimension = "overworld"
                if len(parts) > 4:
                    if parts[4].lower() in self.config["allowed_dimensions"]:
                        dimension = parts[4].lower()
                    elif parts[4].lower() in ["world", "block", "coord"]:
                        coord_type = "world"
                
                # 如果还有第六个参数，检查是否是坐标类型
                if len(parts) > 5 and parts[5].lower() in ["world", "block", "coord"]:
                    coord_type = "world"
                    
            except ValueError as e:
                return f"错误: {str(e)}"
            
            # 验证维度
            if dimension not in self.config["allowed_dimensions"]:
                return f"错误: 不支持的维度 '{dimension}'"
            
            # 检查世界文件夹是否存在
            world_path = self._get_world_path(dimension)
            if not world_path or not os.path.exists(world_path):
                return f"错误: 找不到 {dimension} 维度的世界文件夹"
            
            # 计算区块数量
            min_x, max_x = min(x1, x2), max(x1, x2)
            min_z, max_z = min(z1, z2), max(z1, z2)
            chunk_count = (max_x - min_x + 1) * (max_z - min_z + 1)
            
            if chunk_count > 100:
                return f"错误: 区域过大 ({chunk_count} 个区块)，最多允许删除100个区块"
            
            # 检查是否有区块文件或POI文件存在
            existing_files = 0
            for x in range(min_x, max_x + 1):
                for z in range(min_z, max_z + 1):
                    chunk_file = self._get_chunk_file_path(world_path, x, z)
                    poi_file = self._get_poi_file_path(world_path, x, z)
                    if os.path.exists(chunk_file) or os.path.exists(poi_file):
                        existing_files += 1
            
            if existing_files == 0:
                coord_type_display = "世界坐标" if coord_type == "world" else "区块坐标"
                return f"错误: 指定区域内没有找到任何区块文件或POI文件 ({coord_type_display})"
            
            # 如果需要确认
            if self.config["require_confirmation"]:
                return await self._request_area_confirmation(user_id, x1, z1, x2, z2, dimension, chunk_count, coord_type)
            
            # 直接执行删除
            return await self._execute_area_deletion(user_id, x1, z1, x2, z2, dimension, chunk_count, coord_type)
            
        except Exception as e:
            self.logger.error(f"处理区域删除命令失败: {e}", exc_info=True)
            return f"命令执行失败: {str(e)}"
    
    async def handle_restore_chunk(self, user_id: int, group_id: int, command_text: str,
                                  config_manager=None, **kwargs) -> Optional[str]:
        """处理还原区块命令"""
        try:
            # 检查服务器目录
            if not await self._ensure_server_detected(config_manager):
                return "请先配置服务器工作目录"
            
            # 解析命令参数
            parts = command_text.strip().split()
            
            if len(parts) < 2:
                return "用法: restore_chunk <x> <z> [维度] [coord_type]\n从备份还原区块文件和POI文件\n坐标类型: chunk(区块坐标,默认), world(世界坐标)"
            
            try:
                # 获取坐标类型
                coord_type = self._get_coord_type_from_parts(parts)
                
                # 解析坐标
                x, z = self._parse_coordinates(parts[0], parts[1], coord_type)
                
                # 解析维度
                dimension = "overworld"
                if len(parts) > 2:
                    if parts[2].lower() in self.config["allowed_dimensions"]:
                        dimension = parts[2].lower()
                    elif parts[2].lower() in ["world", "block", "coord"]:
                        coord_type = "world"
                
                # 如果还有第四个参数，检查是否是坐标类型
                if len(parts) > 3 and parts[3].lower() in ["world", "block", "coord"]:
                    coord_type = "world"
                    
            except ValueError as e:
                return f"错误: {str(e)}"
            
            return await self._execute_chunk_restoration(user_id, x, z, dimension, coord_type)
            
        except Exception as e:
            self.logger.error(f"处理还原区块命令失败: {e}", exc_info=True)
            return f"命令执行失败: {str(e)}"
    
    async def handle_backup_chunk(self, user_id: int, group_id: int, command_text: str,
                                 config_manager=None, **kwargs) -> Optional[str]:
        """处理手动备份区块命令"""
        try:
            # 检查服务器目录
            if not await self._ensure_server_detected(config_manager):
                return "请先配置服务器工作目录"
            
            # 解析命令参数
            parts = command_text.strip().split()
            
            if len(parts) < 2:
                return "用法: backup_chunk <x> <z> [维度] [coord_type]\n手动备份区块文件和POI文件\n坐标类型: chunk(区块坐标,默认), world(世界坐标)"
            
            try:
                # 获取坐标类型
                coord_type = self._get_coord_type_from_parts(parts)
                
                # 解析坐标
                x, z = self._parse_coordinates(parts[0], parts[1], coord_type)
                
                # 解析维度
                dimension = "overworld"
                if len(parts) > 2:
                    if parts[2].lower() in self.config["allowed_dimensions"]:
                        dimension = parts[2].lower()
                    elif parts[2].lower() in ["world", "block", "coord"]:
                        coord_type = "world"
                
                # 如果还有第四个参数，检查是否是坐标类型
                if len(parts) > 3 and parts[3].lower() in ["world", "block", "coord"]:
                    coord_type = "world"
                    
            except ValueError as e:
                return f"错误: {str(e)}"
            
            return await self._execute_chunk_backup(user_id, x, z, dimension, coord_type)
            
        except Exception as e:
            self.logger.error(f"处理备份区块命令失败: {e}", exc_info=True)
            return f"命令执行失败: {str(e)}"
    
    async def _request_single_confirmation(self, user_id: int, x: int, z: int, dimension: str, coord_type: str) -> str:
        """请求单个区块删除确认"""
        timeout = self.config["confirmation_timeout"]
        coord_display = f"世界坐标({x*16}, {z*16})" if coord_type == "world" else f"区块坐标({x}, {z})"
        
        confirm_msg = (
            f"确认删除区块文件和POI文件?\n"
            f"坐标: {coord_display}\n"
            f"维度: {dimension}\n"
            f"服务器类型: {self.server_type}\n"
            f"输入: delete_chunk confirm 确认删除\n"
            f"注意: 此操作不可逆，请确保服务器已停止!\n"
            f"请在 {timeout} 秒内确认，超时自动取消"
        )
        
        # 保存待确认的操作
        self.pending_confirmations[user_id] = {
            "user_id": user_id,
            "coordinates": (x, z),
            "dimension": dimension,
            "coord_type": coord_type,
            "type": "single",
            "chunk_count": 1,
            "timestamp": asyncio.get_event_loop().time()
        }
        
        # 创建超时任务
        self._create_confirmation_timeout_task(user_id, timeout)
        
        return confirm_msg
    
    async def _request_area_confirmation(self, user_id: int, x1: int, z1: int, x2: int, z2: int, 
                                       dimension: str, chunk_count: int, coord_type: str) -> str:
        """请求区域区块删除确认"""
        timeout = self.config["confirmation_timeout"]
        coord_display1 = f"世界坐标({x1*16}, {z1*16})" if coord_type == "world" else f"区块坐标({x1}, {z1})"
        coord_display2 = f"世界坐标({x2*16}, {z2*16})" if coord_type == "world" else f"区块坐标({x2}, {z2})"
        
        confirm_msg = (
            f"确认删除区域内的所有区块文件和POI文件?\n"
            f"区域: {coord_display1} 到 {coord_display2}\n"
            f"维度: {dimension}\n"
            f"区块数量: {chunk_count}\n"
            f"服务器类型: {self.server_type}\n"
            f"输入: delete_chunk confirm 确认删除\n"
            f"注意: 此操作不可逆，请确保服务器已停止!\n"
            f"请在 {timeout} 秒内确认，超时自动取消"
        )
        
        # 保存待确认的操作
        self.pending_confirmations[user_id] = {
            "user_id": user_id,
            "coordinates": (x1, z1, x2, z2),
            "dimension": dimension,
            "coord_type": coord_type,
            "type": "area",
            "chunk_count": chunk_count,
            "timestamp": asyncio.get_event_loop().time()
        }
        
        # 创建超时任务
        self._create_confirmation_timeout_task(user_id, timeout)
        
        return confirm_msg
    
    def _create_confirmation_timeout_task(self, user_id: int, timeout: int):
        """创建确认超时任务"""
        # 取消现有的任务
        if user_id in self._confirmation_tasks:
            self._confirmation_tasks[user_id].cancel()
        
        # 创建新任务
        task = asyncio.create_task(self._handle_confirmation_timeout(user_id, timeout))
        self._confirmation_tasks[user_id] = task
    
    async def _handle_confirmation_timeout(self, user_id: int, timeout: int):
        """处理确认超时"""
        try:
            await asyncio.sleep(timeout)
            
            if user_id in self.pending_confirmations:
                operation = self.pending_confirmations.pop(user_id)
                if user_id in self._confirmation_tasks:
                    self._confirmation_tasks.pop(user_id)
                self.logger.info(f"用户 {user_id} 的区块删除操作已超时取消: {operation}")
                
        except asyncio.Cancelled:
            # 任务被取消是正常情况
            pass
        except Exception as e:
            self.logger.error(f"处理确认超时失败: {e}", exc_info=True)
    
    async def _confirm_operation(self, user_id: int, group_id: int) -> str:
        """确认并执行待处理的操作"""
        try:
            if user_id not in self.pending_confirmations:
                return "没有待确认的区块删除操作"
            
            operation = self.pending_confirmations.pop(user_id)
            
            # 取消超时任务
            if user_id in self._confirmation_tasks:
                self._confirmation_tasks[user_id].cancel()
                self._confirmation_tasks.pop(user_id)
            
            if operation["type"] == "single":
                x, z = operation["coordinates"]
                coord_type = operation.get("coord_type", "chunk")
                return await self._execute_chunk_deletion(
                    user_id, x, z, operation["dimension"], "single", coord_type
                )
            else:
                x1, z1, x2, z2 = operation["coordinates"]
                coord_type = operation.get("coord_type", "chunk")
                return await self._execute_area_deletion(
                    user_id, x1, z1, x2, z2, operation["dimension"], 
                    operation["chunk_count"], coord_type
                )
                
        except Exception as e:
            self.logger.error(f"确认操作失败: {e}", exc_info=True)
            return f"确认操作失败: {str(e)}"
    
    async def _execute_chunk_deletion(self, user_id: int, x: int, z: int, 
                                    dimension: str, operation_type: str, coord_type: str = "chunk") -> str:
        """执行单个区块文件和POI文件删除"""
        try:
            # 记录操作开始
            coord_display = f"世界坐标({x*16}, {z*16})" if coord_type == "world" else f"区块坐标({x}, {z})"
            operation_record = {
                "user_id": user_id,
                "type": operation_type,
                "coordinates": coord_display,
                "dimension": dimension,
                "timestamp": self._get_current_time(),
                "success": False
            }
            
            # 获取世界文件夹路径
            world_path = self._get_world_path(dimension)
            if not world_path:
                operation_record["error"] = "找不到世界文件夹路径"
                self.operation_history.append(operation_record)
                return "错误: 找不到世界文件夹路径"
            
            # 获取区块文件和POI文件路径
            chunk_file = self._get_chunk_file_path(world_path, x, z)
            poi_file = self._get_poi_file_path(world_path, x, z)
            
            # 检查文件是否存在
            chunk_exists = os.path.exists(chunk_file)
            poi_exists = os.path.exists(poi_file)
            
            if not chunk_exists and not poi_exists:
                operation_record["error"] = "区块文件和POI文件都不存在"
                self.operation_history.append(operation_record)
                return f"错误: 区块文件和POI文件都不存在 - {dimension} {coord_display}"
            
            # 备份文件（如果启用）
            chunk_backup_path = None
            poi_backup_path = None
            if self.config["backup_before_delete"]:
                if chunk_exists:
                    chunk_backup_path = await self._backup_chunk_file(chunk_file, dimension, x, z, "chunk")
                if poi_exists:
                    poi_backup_path = await self._backup_chunk_file(poi_file, dimension, x, z, "poi")
            
            deleted_chunk = False
            deleted_poi = False
            error_messages = []
            
            # 删除区块文件
            if chunk_exists:
                try:
                    os.remove(chunk_file)
                    deleted_chunk = True
                    self.logger.info(f"已删除区块文件: {chunk_file}")
                except Exception as e:
                    error_messages.append(f"删除区块文件失败: {str(e)}")
                    self.logger.error(f"删除区块文件失败 {chunk_file}: {e}")
            
            # 删除POI文件
            if poi_exists:
                try:
                    os.remove(poi_file)
                    deleted_poi = True
                    self.logger.info(f"已删除POI文件: {poi_file}")
                except Exception as e:
                    error_messages.append(f"删除POI文件失败: {str(e)}")
                    self.logger.error(f"删除POI文件失败 {poi_file}: {e}")
            
            # 记录操作结果
            operation_record["success"] = deleted_chunk or deleted_poi
            operation_record["chunk_deleted"] = deleted_chunk
            operation_record["poi_deleted"] = deleted_poi
            if chunk_backup_path:
                operation_record["chunk_backup"] = chunk_backup_path
            if poi_backup_path:
                operation_record["poi_backup"] = poi_backup_path
            if error_messages:
                operation_record["errors"] = error_messages
            
            self.operation_history.append(operation_record)
            
            # 限制历史记录数量
            if len(self.operation_history) > 100:
                self.operation_history = self.operation_history[-50:]
            
            # 记录用户操作日志
            self.logger.info(f"用户 {user_id} 删除了区块: {dimension} {coord_display} - 区块: {deleted_chunk}, POI: {deleted_poi}")
            
            # 构建结果消息
            result_parts = []
            if deleted_chunk or deleted_poi:
                result_parts.append(f"成功删除 {dimension} {coord_display} 的:")
                if deleted_chunk:
                    result_parts.append("• 区块文件")
                if deleted_poi:
                    result_parts.append("• POI文件")
            else:
                result_parts.append("删除操作失败")
            
            if chunk_backup_path or poi_backup_path:
                result_parts.append("\n已备份:")
                if chunk_backup_path:
                    result_parts.append(f"• 区块文件: {os.path.basename(chunk_backup_path)}")
                if poi_backup_path:
                    result_parts.append(f"• POI文件: {os.path.basename(poi_backup_path)}")
            
            if error_messages:
                result_parts.append("\n错误:")
                result_parts.extend([f"• {msg}" for msg in error_messages])
            
            return "\n".join(result_parts)
            
        except Exception as e:
            # 记录失败操作
            operation_record["success"] = False
            operation_record["error"] = str(e)
            self.operation_history.append(operation_record)
            
            self.logger.error(f"删除区块失败: {e}", exc_info=True)
            return f"删除区块失败: {str(e)}"
    
    async def _execute_area_deletion(self, user_id: int, x1: int, z1: int, 
                                   x2: int, z2: int, dimension: str, chunk_count: int, coord_type: str = "chunk") -> str:
        """执行区域区块文件和POI文件删除"""
        try:
            # 记录操作开始
            coord_display = f"世界坐标({x1*16},{z1*16})-({x2*16},{z2*16})" if coord_type == "world" else f"区块坐标({x1},{z1})-({x2},{z2})"
            operation_record = {
                "user_id": user_id,
                "type": "area",
                "coordinates": coord_display,
                "dimension": dimension,
                "chunk_count": chunk_count,
                "timestamp": self._get_current_time(),
                "success": False
            }
            
            # 获取世界文件夹路径
            world_path = self._get_world_path(dimension)
            if not world_path:
                operation_record["error"] = "找不到世界文件夹路径"
                self.operation_history.append(operation_record)
                return "错误: 找不到世界文件夹路径"
            
            # 计算实际范围
            min_x, max_x = min(x1, x2), max(x1, x2)
            min_z, max_z = min(z1, z2), max(z1, z2)
            
            deleted_chunk_count = 0
            deleted_poi_count = 0
            backup_chunk_count = 0
            backup_poi_count = 0
            error_chunk_count = 0
            error_poi_count = 0
            
            # 遍历所有区块并删除
            for x in range(min_x, max_x + 1):
                for z in range(min_z, max_z + 1):
                    chunk_file = self._get_chunk_file_path(world_path, x, z)
                    poi_file = self._get_poi_file_path(world_path, x, z)
                    
                    # 备份文件（如果启用）
                    if self.config["backup_before_delete"]:
                        if os.path.exists(chunk_file):
                            await self._backup_chunk_file(chunk_file, dimension, x, z, "chunk")
                            backup_chunk_count += 1
                        if os.path.exists(poi_file):
                            await self._backup_chunk_file(poi_file, dimension, x, z, "poi")
                            backup_poi_count += 1
                    
                    # 删除区块文件
                    if os.path.exists(chunk_file):
                        try:
                            os.remove(chunk_file)
                            deleted_chunk_count += 1
                            self.logger.debug(f"已删除区块文件: {chunk_file}")
                        except Exception as e:
                            error_chunk_count += 1
                            self.logger.warning(f"删除区块文件失败 {chunk_file}: {e}")
                    
                    # 删除POI文件
                    if os.path.exists(poi_file):
                        try:
                            os.remove(poi_file)
                            deleted_poi_count += 1
                            self.logger.debug(f"已删除POI文件: {poi_file}")
                        except Exception as e:
                            error_poi_count += 1
                            self.logger.warning(f"删除POI文件失败 {poi_file}: {e}")
            
            # 记录操作结果
            operation_record["success"] = deleted_chunk_count > 0 or deleted_poi_count > 0
            operation_record["deleted_chunk_count"] = deleted_chunk_count
            operation_record["deleted_poi_count"] = deleted_poi_count
            operation_record["backup_chunk_count"] = backup_chunk_count
            operation_record["backup_poi_count"] = backup_poi_count
            operation_record["error_chunk_count"] = error_chunk_count
            operation_record["error_poi_count"] = error_poi_count
            self.operation_history.append(operation_record)
            
            # 记录用户操作日志
            self.logger.info(
                f"用户 {user_id} 删除了区域: {dimension} {coord_display} "
                f"共 {deleted_chunk_count} 个区块文件, {deleted_poi_count} 个POI文件"
            )
            
            result_msg = [
                f"区域删除完成",
                f"坐标范围: {coord_display}",
                f"统计:",
                f"• 删除 {deleted_chunk_count} 个区块文件",
                f"• 删除 {deleted_poi_count} 个POI文件",
                f"备份:",
                f"• {backup_chunk_count} 个区块文件",
                f"• {backup_poi_count} 个POI文件",
                f"错误:",
                f"• {error_chunk_count} 个区块文件删除失败",
                f"• {error_poi_count} 个POI文件删除失败"
            ]
            
            if deleted_chunk_count == 0 and deleted_poi_count == 0:
                result_msg[0] = "区域删除完成"
                result_msg.append("提示: 指定区域内没有找到可删除的文件")
            
            return "\n".join(result_msg)
            
        except Exception as e:
            # 记录失败操作
            operation_record["success"] = False
            operation_record["error"] = str(e)
            self.operation_history.append(operation_record)
            
            self.logger.error(f"删除区域区块失败: {e}", exc_info=True)
            return f"删除区域区块失败: {str(e)}"
    
    async def _execute_chunk_restoration(self, user_id: int, x: int, z: int, dimension: str, coord_type: str = "chunk") -> str:
        """执行区块文件和POI文件还原"""
        try:
            coord_display = f"世界坐标({x*16}, {z*16})" if coord_type == "world" else f"区块坐标({x}, {z})"
            
            # 获取世界文件夹路径
            world_path = self._get_world_path(dimension)
            if not world_path:
                return "错误: 找不到世界文件夹路径"
            
            # 获取区块文件和POI文件路径
            chunk_file = self._get_chunk_file_path(world_path, x, z)
            poi_file = self._get_poi_file_path(world_path, x, z)
            
            # 获取备份文件路径
            chunk_backup_file = await self._get_backup_file_path(dimension, x, z, "chunk")
            poi_backup_file = await self._get_backup_file_path(dimension, x, z, "poi")
            
            restored_chunk = False
            restored_poi = False
            error_messages = []
            
            # 还原区块文件
            if chunk_backup_file and os.path.exists(chunk_backup_file):
                try:
                    # 确保目标目录存在
                    os.makedirs(os.path.dirname(chunk_file), exist_ok=True)
                    shutil.copy2(chunk_backup_file, chunk_file)
                    restored_chunk = True
                    self.logger.info(f"已还原区块文件: {chunk_file}")
                except Exception as e:
                    error_messages.append(f"还原区块文件失败: {str(e)}")
                    self.logger.error(f"还原区块文件失败 {chunk_backup_file} -> {chunk_file}: {e}")
            elif chunk_backup_file:
                error_messages.append("区块备份文件不存在")
            
            # 还原POI文件
            if poi_backup_file and os.path.exists(poi_backup_file):
                try:
                    # 确保目标目录存在
                    os.makedirs(os.path.dirname(poi_file), exist_ok=True)
                    shutil.copy2(poi_backup_file, poi_file)
                    restored_poi = True
                    self.logger.info(f"已还原POI文件: {poi_file}")
                except Exception as e:
                    error_messages.append(f"还原POI文件失败: {str(e)}")
                    self.logger.error(f"还原POI文件失败 {poi_backup_file} -> {poi_file}: {e}")
            elif poi_backup_file:
                error_messages.append("POI备份文件不存在")
            
            # 构建结果消息
            result_parts = []
            if restored_chunk or restored_poi:
                result_parts.append(f"成功还原 {dimension} {coord_display} 的:")
                if restored_chunk:
                    result_parts.append("• 区块文件")
                if restored_poi:
                    result_parts.append("• POI文件")
            else:
                result_parts.append("还原操作失败")
            
            if error_messages:
                result_parts.append("\n错误:")
                result_parts.extend([f"• {msg}" for msg in error_messages])
            
            if not chunk_backup_file and not poi_backup_file:
                result_parts.append("\n提示: 没有找到对应的备份文件")
            
            return "\n".join(result_parts)
            
        except Exception as e:
            self.logger.error(f"还原区块失败: {e}", exc_info=True)
            return f"还原区块失败: {str(e)}"
    
    async def _execute_chunk_backup(self, user_id: int, x: int, z: int, dimension: str, coord_type: str = "chunk") -> str:
        """执行手动备份区块文件和POI文件"""
        try:
            coord_display = f"世界坐标({x*16}, {z*16})" if coord_type == "world" else f"区块坐标({x}, {z})"
            
            # 获取世界文件夹路径
            world_path = self._get_world_path(dimension)
            if not world_path:
                return "错误: 找不到世界文件夹路径"
            
            # 获取区块文件和POI文件路径
            chunk_file = self._get_chunk_file_path(world_path, x, z)
            poi_file = self._get_poi_file_path(world_path, x, z)
            
            # 检查文件是否存在
            chunk_exists = os.path.exists(chunk_file)
            poi_exists = os.path.exists(poi_file)
            
            if not chunk_exists and not poi_exists:
                return f"错误: 区块文件和POI文件都不存在 - {dimension} {coord_display}"
            
            # 备份文件
            chunk_backup_path = None
            poi_backup_path = None
            
            if chunk_exists:
                chunk_backup_path = await self._backup_chunk_file(chunk_file, dimension, x, z, "chunk")
            
            if poi_exists:
                poi_backup_path = await self._backup_chunk_file(poi_file, dimension, x, z, "poi")
            
            # 构建结果消息
            result_parts = [f"手动备份完成 {dimension} {coord_display}:"]
            
            if chunk_backup_path:
                result_parts.append(f"• 区块文件: {os.path.basename(chunk_backup_path)}")
            
            if poi_backup_path:
                result_parts.append(f"• POI文件: {os.path.basename(poi_backup_path)}")
            
            if not chunk_backup_path and not poi_backup_path:
                result_parts.append("• 没有文件需要备份")
            
            return "\n".join(result_parts)
            
        except Exception as e:
            self.logger.error(f"备份区块失败: {e}", exc_info=True)
            return f"备份区块失败: {str(e)}"
    
    async def _get_backup_file_path(self, dimension: str, x: int, z: int, file_type: str) -> Optional[str]:
        """获取备份文件路径"""
        try:
            backup_dir = os.path.join(self.server_working_directory, "chunk_backups", dimension, file_type)
            if not os.path.exists(backup_dir):
                return None
            
            # 查找对应的备份文件
            region_x = x // 32
            region_z = z // 32
            backup_filename = f"r.{region_x}.{region_z}.mca.backup"
            backup_path = os.path.join(backup_dir, backup_filename)
            
            return backup_path if os.path.exists(backup_path) else None
            
        except Exception as e:
            self.logger.warning(f"获取备份文件路径失败: {e}")
            return None
    
    def _detect_server_type(self, working_dir: str) -> str:
        """检测服务器类型"""
        # 检查服务端jar文件
        jar_files = list(Path(working_dir).glob("*.jar"))
        for jar_file in jar_files:
            jar_name = jar_file.name.lower()
            if "paper" in jar_name or "purpur" in jar_name or "spigot" in jar_name or "bukkit" in jar_name:
                return "plugin"
            elif "fabric" in jar_name or "forge" in jar_name:
                return "modded"
        
        # 检查文件夹结构
        if (Path(working_dir) / "world_nether").exists() or (Path(working_dir) / "world_the_end").exists():
            return "plugin"
        elif (Path(working_dir) / "world" / "DIM-1").exists() or (Path(working_dir) / "world" / "DIM1").exists():
            return "modded"
        
        return "vanilla"
    
    def _get_world_path(self, dimension: str) -> Optional[str]:
        """获取世界文件夹完整路径"""
        if not self.server_working_directory:
            return None
        
        structure = self.SERVER_WORLD_STRUCTURES.get(self.server_type, {})
        world_relative_path = structure.get(dimension)
        
        if not world_relative_path:
            return None
        
        return os.path.join(self.server_working_directory, world_relative_path)
    
    def _get_chunk_file_path(self, world_path: str, chunk_x: int, chunk_z: int) -> str:
        """获取区块文件路径"""
        # Minecraft区块文件存储格式: region/r.x.z.mca
        region_dir = os.path.join(world_path, "region")
        region_x = chunk_x // 32
        region_z = chunk_z // 32
        filename = f"r.{region_x}.{region_z}.mca"
        
        return os.path.join(region_dir, filename)
    
    def _get_poi_file_path(self, world_path: str, chunk_x: int, chunk_z: int) -> str:
        """获取POI文件路径"""
        # POI文件存储格式: poi/r.x.z.mca
        poi_dir = os.path.join(world_path, "poi")
        region_x = chunk_x // 32
        region_z = chunk_z // 32
        filename = f"r.{region_x}.{region_z}.mca"
        
        return os.path.join(poi_dir, filename)
    
    async def _backup_chunk_file(self, chunk_file: str, dimension: str, x: int, z: int, file_type: str = "chunk") -> str:
        """备份区块文件或POI文件"""
        try:
            # 根据文件类型创建不同的备份文件夹
            backup_dir = os.path.join(self.server_working_directory, "chunk_backups", dimension, file_type)
            os.makedirs(backup_dir, exist_ok=True)
            
            # 使用原文件名加上 .backup 后缀
            original_filename = os.path.basename(chunk_file)
            backup_filename = f"{original_filename}.backup"
            backup_path = os.path.join(backup_dir, backup_filename)
            
            shutil.copy2(chunk_file, backup_path)
            self.logger.info(f"已备份{file_type}文件: {backup_path}")
            
            return backup_path
            
        except Exception as e:
            self.logger.warning(f"备份{file_type}文件失败 {chunk_file}: {e}")
            return ""
    
    async def _ensure_server_detected(self, config_manager) -> bool:
        """确保服务器已检测"""
        if not self.server_working_directory and config_manager:
            # 自动检测服务器
            working_dir = config_manager.get_server_working_directory()
            start_script = config_manager.get_server_start_script()
            
            if not working_dir and start_script:
                working_dir = os.path.dirname(start_script)
            
            if working_dir and os.path.exists(working_dir):
                self.server_working_directory = working_dir
                self.server_type = self._detect_server_type(working_dir)
                return True
        
        return bool(self.server_working_directory)
    
    def _get_current_time(self) -> str:
        """获取当前时间字符串"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    async def on_server_started(self) -> None:
        """服务器启动事件处理"""
        self.logger.info("服务器已启动，区块删除插件就绪")
        # 清理所有待确认操作
        self.pending_confirmations.clear()
        for task in self._confirmation_tasks.values():
            task.cancel()
        self._confirmation_tasks.clear()
    
    async def on_server_stopping(self) -> None:
        """服务器停止事件处理"""
        self.logger.info("服务器正在停止，清理区块删除插件状态")
        self.pending_confirmations.clear()
        for task in self._confirmation_tasks.values():
            task.cancel()
        self._confirmation_tasks.clear()
    
    def get_plugin_help(self) -> str:
        """返回插件帮助信息"""
        return f"""
{self.name} v{self.version}
作者: {self.author}
{self.description}

重要提示:
• 请在服务器停止状态下使用
• 会同时删除区块文件(region)和POI文件(poi)，保持数据一致性

命令列表:
• delete_chunk <x> <z> [维度] [coord_type] - 删除指定坐标的区块文件和POI文件
• delete_chunk_area <x1> <z1> <x2> <z2> [维度] [coord_type] - 删除区域内的所有区块文件和POI文件
• restore_chunk <x> <z> [维度] [coord_type] - 从备份还原区块文件和POI文件
• backup_chunk <x> <z> [维度] [coord_type] - 手动备份区块文件和POI文件

坐标类型:
• chunk - 区块坐标 (默认)
• world - 世界坐标 (会自动转换为区块坐标)

支持维度: {', '.join(self.config['allowed_dimensions'])}
安全特性: 备份功能 {'已启用' if self.config['backup_before_delete'] else '已禁用'}
需要确认: {'是' if self.config['require_confirmation'] else '否'}
确认超时: {self.config['confirmation_timeout']} 秒

示例:
• delete_chunk 10 20 overworld - 删除主世界区块坐标(10,20)
• delete_chunk 160 320 overworld world - 删除主世界世界坐标(160,320)对应的区块
• restore_chunk 10 20 nether - 还原下界区块坐标(10,20)的备份
• backup_chunk 30 40 end - 手动备份末地区块坐标(30,40)
"""