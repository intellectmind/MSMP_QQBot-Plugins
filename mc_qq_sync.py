import json
import re
import time
import asyncio
from pathlib import Path
from typing import Dict, Optional, List, Any
from datetime import datetime
from plugin_manager import BotPlugin


class MCQQSyncPlugin(BotPlugin):
    """MC-QQ消息同步插件"""
    
    name = "MC-QQ消息同步"
    version = "1.0.0"
    author = "MSMP_QQBot"
    description = "实现MC玩家消息与QQ群内消息的双向同步"
    
    def __init__(self, logger):
        super().__init__(logger)
        self.plugin_manager = None
        self.qq_mc_binding_plugin = None
        
        # 配置文件和数据路径
        self.plugin_dir = Path("plugins/mc_qq_sync")
        self.config_file = self.plugin_dir / "config.json"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        
        # 加载配置
        self.config = self._load_config()
        
        # 消息缓存（用于防止重复处理）
        self.message_cache: Dict[str, float] = {}
        self._cache_timeout = 5  # 缓存5秒
        
        # 日志监控相关
        self._last_processed_log_index = 0
        self._log_check_task = None
        self._running = False
        self._server_running = False
        self._processed_log_timestamps = set()
        
        # 聊天消息匹配模式
        self.chat_message_pattern = r'.*\[Not Secure\]\s*<([^>]+)>\s*(.+)'
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件，不存在则创建默认配置"""
        default_config = {
            # 功能开关 - 独立分开
            'features': {
                'mc_auto_sync_to_qq': {  # MC玩家自动同步到QQ群
                    'enabled': False,
                    'group_ids': [123456789]
                },
                'mc_manual_sync_to_qq': {  # MC玩家主动发送消息到QQ群（使用qq命令）
                    'enabled': True,
                    'group_ids': [123456789]
                },
                'qq_manual_to_mc': {  # QQ群用户通过命令发送消息到MC（使用mc命令）
                    'enabled': True,
                    'group_ids': [123456789]
                }
            },
            # 消息格式
            'message_format': {
                'mc_auto_to_qq': '[MC] {player}: {message}',      # MC自动同步消息格式
                'mc_manual_to_qq': '[MC] {player}: {message}',    # MC主动发送消息格式
                'qq_manual_to_mc': '[QQ] {nickname}: {message}'        # QQ用户命令消息格式
            },
            # QQ命令配置
            'qq_commands': {
                'mc_command_prefix': 'mc'                # QQ发送到MC的命令前缀
            },
            # MC游戏内命令配置
            'mc_commands': {
                'qq_command_prefix': 'qq'               # MC发送到QQ的命令前缀
            },
            # 黑名单
            'blacklist': {
                'players': [],      # 被屏蔽的玩家
                'users': []         # 被屏蔽的QQ用户
            }
        }
        
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                
                merged_config = default_config.copy()
                merged_config.update(loaded_config)
                
                self.logger.info(f"已加载配置文件: {self.config_file}")
                return merged_config
            else:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, ensure_ascii=False, indent=2)
                self.logger.info(f"已创建默认配置文件: {self.config_file}")
                return default_config
                
        except Exception as e:
            self.logger.error(f"加载配置文件失败: {e}，使用默认配置")
            return default_config
    
    def _save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            self.logger.debug("配置文件已保存")
        except Exception as e:
            self.logger.error(f"保存配置文件失败: {e}")
    
    async def on_load(self, plugin_manager) -> bool:
        """插件加载"""
        try:
            self.plugin_manager = plugin_manager
            
            # 获取QQ-MC绑定插件
            self.qq_mc_binding_plugin = plugin_manager.find_plugin_by_name("qq_mc_binding")
            if not self.qq_mc_binding_plugin:
                self.logger.warning("未找到qq_mc_binding插件，部分功能将不可用")
            
            # 注册事件监听
            plugin_manager.register_event_listener("server_started", self.on_server_started)
            plugin_manager.register_event_listener("server_stopping", self.on_server_stopping)
            
            # 注册QQ命令（QQ群内发送消息到MC）
            plugin_manager.register_command(
                command_name="mc_message",
                handler=self.handle_mc_command,
                names=[self.config['qq_commands']['mc_command_prefix']],
                description="发送消息到MC服务器",
                usage=f"{self.config['qq_commands']['mc_command_prefix']} <消息内容>"
            )
            
            plugin_manager.register_command(
                command_name="sync_config",
                handler=self.handle_sync_config_command,
                names=["sync_config"],
                description="管理消息同步配置",
                usage="sync_config show/enable/disable/addgroup/removegroup",
                admin_only=True
            )
            
            # 启动日志轮询任务
            self._running = True
            self._log_check_task = asyncio.create_task(self._log_polling_loop())
            
            self.logger.info("MC-QQ消息同步插件已加载")
            return True
        
        except Exception as e:
            self.logger.error(f"插件加载失败: {e}", exc_info=True)
            return False
    
    async def on_unload(self):
        """插件卸载"""
        try:
            self._running = False
            self._server_running = False
            if self._log_check_task:
                self._log_check_task.cancel()
                try:
                    await self._log_check_task
                except asyncio.CancelledError:
                    pass
            
            self._save_config()
            self.logger.info("MC-QQ消息同步插件已卸载")
        except Exception as e:
            self.logger.error(f"插件卸载失败: {e}", exc_info=True)
    
    async def on_config_reload(self, old_config: Dict, new_config: Dict):
        """配置重新加载"""
        try:
            if 'mc_qq_sync' in new_config:
                new_plugin_config = new_config['mc_qq_sync']
                self.config.update(new_plugin_config)
                self._save_config()
                self.logger.info("插件配置已更新")
        except Exception as e:
            self.logger.error(f"配置更新失败: {e}")
    
    async def on_server_started(self, *args, **kwargs):
        """服务器启动事件"""
        self.logger.info("服务器已启动，开始同步消息")
        self._server_running = True
        self._last_processed_log_index = 0
    
    async def on_server_stopping(self, *args, **kwargs):
        """服务器停止事件"""
        self.logger.info("服务器正在停止，暂停同步消息")
        self._server_running = False
    
    async def _log_polling_loop(self):
        """日志轮询循环"""
        while self._running:
            try:
                if not self._server_running:
                    self._check_server_status()
                    if not self._server_running:
                        await asyncio.sleep(5)
                        continue
                
                if not self.plugin_manager:
                    await asyncio.sleep(5)
                    continue
                
                # 检查MC玩家消息（自动同步或主动发送功能）
                auto_enabled = self.config['features']['mc_auto_sync_to_qq']['enabled']
                manual_enabled = self.config['features']['mc_manual_sync_to_qq']['enabled']
                
                if auto_enabled or manual_enabled:
                    await self._check_server_logs()
                
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"日志轮询出错: {e}", exc_info=True)
                await asyncio.sleep(5)
    
    def _check_server_status(self):
        """检查服务器运行状态"""
        try:
            if hasattr(self.plugin_manager, 'is_server_running'):
                self._server_running = self.plugin_manager.is_server_running()
            else:
                self._server_running = True
        except Exception as e:
            self.logger.error(f"检查服务器状态失败: {e}")
            self._server_running = True
    
    async def _check_server_logs(self):
        """检查服务器日志中的玩家消息"""
        try:
            self._cleanup_expired_cache()
            
            if not self.plugin_manager:
                return
            
            logs = self.plugin_manager.get_server_logs(50)
            
            if not isinstance(logs, list) or not logs:
                return
            
            for log_line in reversed(logs):
                if isinstance(log_line, str):
                    log_hash = self._get_log_hash(log_line)
                    
                    if log_hash in self._processed_log_timestamps:
                        continue
                    
                    processed = await self._process_player_message(log_line)
                    if processed:
                        self._processed_log_timestamps.add(log_hash)
            
            self._cleanup_processed_logs_cache()
            
        except Exception as e:
            self.logger.error(f"检查服务器日志失败: {e}")
    
    def _get_log_hash(self, log_line: str) -> str:
        """生成日志的唯一标识"""
        try:
            import hashlib
            return hashlib.md5(log_line.encode('utf-8')).hexdigest()
        except:
            return log_line
    
    def _cleanup_processed_logs_cache(self):
        """清理已处理日志缓存"""
        try:
            if len(self._processed_log_timestamps) > 200:
                timestamps_list = list(self._processed_log_timestamps)
                remove_count = len(timestamps_list) // 2
                for i in range(remove_count):
                    self._processed_log_timestamps.discard(timestamps_list[i])
        except Exception as e:
            self.logger.error(f"清理日志缓存失败: {e}")
    
    def _cleanup_expired_cache(self):
        """清理过期的消息缓存"""
        try:
            current_time = time.time()
            expired_keys = [
                key for key, timestamp in self.message_cache.items()
                if current_time - timestamp > self._cache_timeout
            ]
            for key in expired_keys:
                del self.message_cache[key]
        except Exception as e:
            self.logger.error(f"清理消息缓存失败: {e}")
    
    async def _process_player_message(self, log_line: str) -> bool:
        """处理玩家消息"""
        try:
            if '[Not Secure]' not in log_line:
                return False
            
            pattern = self.chat_message_pattern
            match = re.search(pattern, log_line)
            
            if not match:
                return False
            
            player_name = match.group(1).strip()
            message = match.group(2).strip()
            
            self.logger.debug(f"捕获到玩家消息: {player_name} -> {message}")
            
            # 检查玩家是否在黑名单中
            if player_name in self.config['blacklist']['players']:
                self.logger.debug(f"玩家 {player_name} 在黑名单中，跳过处理")
                return False
            
            # 检查是否是 qq 命令（主动发送消息到QQ）
            if message.startswith('qq '):
                qq_message = message[3:].strip()
                if not qq_message:
                    self.logger.debug(f"玩家 {player_name} 的qq命令消息为空，跳过处理")
                    return False
                
                # 检查主动发送到QQ功能是否启用
                if not self.config['features']['mc_manual_sync_to_qq']['enabled']:
                    self.logger.debug("MC主动发送到QQ功能已禁用")
                    return False
                
                # 检查群列表是否为空
                if not self.config['features']['mc_manual_sync_to_qq']['group_ids']:
                    self.logger.debug("MC主动发送到QQ的群列表为空")
                    return False
                
                cache_key = f"{player_name}:qq:{qq_message}"
                if cache_key in self.message_cache:
                    self.logger.debug(f"消息缓存中已存在: {cache_key}，跳过处理")
                    return False
                
                self.message_cache[cache_key] = time.time()
                self.logger.info(f"处理MC主动发送到QQ: {player_name} -> {qq_message}")
                await self._forward_player_message_to_qq(player_name, qq_message, 'mc_manual_to_qq')
                return True
            
            # 过滤其他命令
            if message.startswith('/') or message.startswith('mc '):
                self.logger.debug(f"玩家 {player_name} 发送了命令，跳过处理: {message}")
                return False
            
            # 检查自动同步是否可用
            if not self.config['features']['mc_auto_sync_to_qq']['enabled']:
                self.logger.debug("MC自动同步到QQ功能已禁用")
                return False
            
            # 检查群列表是否为空
            if not self.config['features']['mc_auto_sync_to_qq']['group_ids']:
                self.logger.debug("MC自动同步到QQ的群列表为空")
                return False
            
            cache_key = f"{player_name}:{message}"
            if cache_key in self.message_cache:
                self.logger.debug(f"消息缓存中已存在: {cache_key}，跳过处理")
                return False
            
            self.message_cache[cache_key] = time.time()
            self.logger.info(f"处理MC自动同步到QQ: {player_name} -> {message}")
            await self._forward_player_message_to_qq(player_name, message, 'mc_auto_to_qq')
            return True
            
        except Exception as e:
            self.logger.error(f"处理玩家消息失败: {e}", exc_info=True)
            return False
    
    async def _forward_player_message_to_qq(self, player_name: str, message: str, msg_type: str = 'mc_auto_to_qq'):
        """将玩家消息转发到QQ群"""
        try:
            # 根据消息类型确定功能和群列表
            if msg_type == 'mc_auto_to_qq':
                feature_config = self.config['features']['mc_auto_sync_to_qq']
            elif msg_type == 'mc_manual_to_qq':
                feature_config = self.config['features']['mc_manual_sync_to_qq']
            else:
                return
            
            if not feature_config['enabled']:
                return
            
            group_ids = feature_config['group_ids']
            if not group_ids:
                return
            
            if not self.plugin_manager.qq_server:
                return
            
            formatted_message = self.config['message_format'][msg_type].format(
                player=player_name,
                message=message
            )
            
            for group_id in group_ids:
                try:
                    await self.plugin_manager.qq_server.send_group_message(
                        self.plugin_manager.qq_server.current_connection,
                        group_id,
                        formatted_message
                    )
                    self.logger.debug(f"已转发玩家消息到群 {group_id}")
                except Exception as e:
                    self.logger.error(f"转发消息到群 {group_id} 失败: {e}")
            
        except Exception as e:
            self.logger.error(f"转发玩家消息失败: {e}")
    
    async def handle_mc_command(self, user_id: int, group_id: int,
                               command_text: str, **kwargs) -> Optional[str]:
        """处理QQ命令：通过mc命令发送消息到MC服务器"""
        try:
            self.logger.info(f"收到QQ命令: 用户{user_id} 在群{group_id} 发送: {command_text}")
            
            # 检查QQ->MC功能是否启用
            if not self.config['features']['qq_manual_to_mc']['enabled']:
                msg = f"[CQ:at,qq={user_id}] QQ->MC消息功能已禁用"
                self.logger.debug(msg)
                return msg
            
            # 检查群是否在允许列表
            if group_id not in self.config['features']['qq_manual_to_mc']['group_ids']:
                msg = f"[CQ:at,qq={user_id}] 本群未启用该功能"
                self.logger.debug(f"{msg} (群ID: {group_id}, 允许群: {self.config['features']['qq_manual_to_mc']['group_ids']})")
                return msg
            
            # 检查用户是否绑定游戏ID
            if self.qq_mc_binding_plugin:
                qq_id = str(user_id)
                game_id = self._get_game_id_by_qq(qq_id)
                if not game_id:
                    msg = (
                        f"[CQ:at,qq={user_id}] 你还未绑定游戏ID\n"
                        f"请先使用 绑定 命令进行绑定"
                    )
                    self.logger.debug(msg)
                    return msg
            else:
                self.logger.warning("qq_mc_binding插件未加载，无法检查游戏ID绑定")
            
            # 检查用户是否在黑名单中
            if user_id in self.config['blacklist']['users']:
                msg = f"[CQ:at,qq={user_id}] 你无权使用此功能"
                self.logger.debug(msg)
                return msg
            
            # 检查消息是否为空
            if not command_text or not command_text.strip():
                msg = f"[CQ:at,qq={user_id}] 请输入要发送的消息\n用法: mc <消息内容>"
                self.logger.debug(msg)
                return msg
            
            message = command_text.strip()
            qq_nickname = kwargs.get('nickname', str(user_id))
            
            # 获取绑定的游戏ID
            game_id = None
            if self.qq_mc_binding_plugin:
                game_id = self._get_game_id_by_qq(str(user_id))
            
            # 格式化消息
            if game_id:
                formatted_message = f"《{game_id}》{message}"
            else:
                formatted_message = f"《QQ {user_id}》{message}"
            
            # 发送到MC
            self.logger.debug(f"正在发送消息到MC: {formatted_message}")
            await self._send_message_to_mc(formatted_message)
            
            self.logger.info(f"QQ {user_id}({qq_nickname}) 发送消息到MC: {message}")
            return f"[CQ:at,qq={user_id}] 消息已发送到MC服务器"
            
        except Exception as e:
            self.logger.error(f"处理MC命令失败: {e}", exc_info=True)
            return f"[CQ:at,qq={user_id}] 处理失败，请稍后重试"
    
    def _get_game_id_by_qq(self, qq_id: str) -> Optional[str]:
        """通过QQ号获取绑定的游戏ID"""
        try:
            if not self.qq_mc_binding_plugin:
                return None
            
            if hasattr(self.qq_mc_binding_plugin, 'binding_data'):
                binding_data = self.qq_mc_binding_plugin.binding_data
                if qq_id in binding_data and binding_data[qq_id]:
                    return binding_data[qq_id][0].get('game_id')
            
            return None
        except Exception as e:
            self.logger.error(f"获取游戏ID失败: {e}")
            return None
    
    async def _send_message_to_mc(self, message: str):
        """发送消息到MC服务器"""
        try:
            self.logger.debug(f"_send_message_to_mc 被调用，消息: {message}")
            
            if not self.plugin_manager:
                self.logger.error("plugin_manager 为 None")
                return
            
            if not self.plugin_manager.qq_server:
                self.logger.error("qq_server 为 None")
                return
            
            if not hasattr(self.plugin_manager.qq_server, 'rcon_client'):
                self.logger.error("qq_server 没有 rcon_client 属性")
                return
            
            rcon = self.plugin_manager.qq_server.rcon_client
            if not rcon:
                self.logger.error("rcon_client 为 None")
                return
            
            if not rcon.is_connected():
                self.logger.warning("RCON连接未建立")
                return
            
            cmd = f"say {message}"
            self.logger.debug(f"执行RCON命令: {cmd}")
            rcon.execute_command(cmd)
            self.logger.info(f"已通过RCON发送消息到MC: {message}")
            
        except Exception as e:
            self.logger.error(f"发送消息到MC失败: {e}", exc_info=True)
    
    async def handle_sync_config_command(self, user_id: int, group_id: int,
                                        command_text: str, **kwargs) -> Optional[str]:
        """处理同步配置命令"""
        try:
            args = command_text.strip().split() if command_text else []
            
            if not args:
                return self._get_sync_config_info()
            
            command = args[0].lower()
            
            if command == 'show':
                return self._get_sync_config_info()
            
            elif command == 'enable':
                if len(args) < 2:
                    return "用法: sync_config enable <auto_mc|manual_mc|manual_qq>"
                
                mode = args[1].lower()
                if mode == 'auto_mc':
                    self.config['features']['mc_auto_sync_to_qq']['enabled'] = True
                    self._save_config()
                    return "已启用MC玩家自动同步到QQ"
                elif mode == 'manual_mc':
                    self.config['features']['mc_manual_sync_to_qq']['enabled'] = True
                    self._save_config()
                    return "已启用MC玩家主动发送消息到QQ"
                elif mode == 'manual_qq':
                    self.config['features']['qq_manual_to_mc']['enabled'] = True
                    self._save_config()
                    return "已启用QQ用户通过命令发送消息到MC"
                else:
                    return "无效的模式"
            
            elif command == 'disable':
                if len(args) < 2:
                    return "用法: sync_config disable <auto_mc|manual_mc|manual_qq>"
                
                mode = args[1].lower()
                if mode == 'auto_mc':
                    self.config['features']['mc_auto_sync_to_qq']['enabled'] = False
                    self._save_config()
                    return "已禁用MC玩家自动同步到QQ"
                elif mode == 'manual_mc':
                    self.config['features']['mc_manual_sync_to_qq']['enabled'] = False
                    self._save_config()
                    return "已禁用MC玩家主动发送消息到QQ"
                elif mode == 'manual_qq':
                    self.config['features']['qq_manual_to_mc']['enabled'] = False
                    self._save_config()
                    return "已禁用QQ用户通过命令发送消息到MC"
                else:
                    return "无效的模式"
            
            elif command == 'addgroup':
                if len(args) < 3:
                    return "用法: sync_config addgroup <auto_mc|manual_mc|manual_qq> <group_id>"
                
                mode = args[1].lower()
                try:
                    group_id_to_add = int(args[2])
                except ValueError:
                    return "group_id必须是数字"
                
                feature_key = None
                if mode == 'auto_mc':
                    feature_key = 'mc_auto_sync_to_qq'
                    description = "MC自动同步到QQ"
                elif mode == 'manual_mc':
                    feature_key = 'mc_manual_sync_to_qq'
                    description = "MC主动发送消息到QQ"
                elif mode == 'manual_qq':
                    feature_key = 'qq_manual_to_mc'
                    description = "QQ用户命令发送消息到MC"
                else:
                    return "无效的模式"
                
                if group_id_to_add not in self.config['features'][feature_key]['group_ids']:
                    self.config['features'][feature_key]['group_ids'].append(group_id_to_add)
                    self._save_config()
                    return f"已添加群{group_id_to_add}到{description}列表"
                else:
                    return f"群{group_id_to_add}已在{description}列表中"
            
            elif command == 'removegroup':
                if len(args) < 3:
                    return "用法: sync_config removegroup <auto_mc|manual_mc|manual_qq> <group_id>"
                
                mode = args[1].lower()
                try:
                    group_id_to_remove = int(args[2])
                except ValueError:
                    return "group_id必须是数字"
                
                feature_key = None
                if mode == 'auto_mc':
                    feature_key = 'mc_auto_sync_to_qq'
                    description = "MC自动同步到QQ"
                elif mode == 'manual_mc':
                    feature_key = 'mc_manual_sync_to_qq'
                    description = "MC主动发送消息到QQ"
                elif mode == 'manual_qq':
                    feature_key = 'qq_manual_to_mc'
                    description = "QQ用户命令发送消息到MC"
                else:
                    return "无效的模式"
                
                if group_id_to_remove in self.config['features'][feature_key]['group_ids']:
                    self.config['features'][feature_key]['group_ids'].remove(group_id_to_remove)
                    self._save_config()
                    return f"已从{description}列表中移除群{group_id_to_remove}"
                else:
                    return f"群{group_id_to_remove}不在{description}列表中"
            
            else:
                return "未知的命令"
        
        except Exception as e:
            self.logger.error(f"处理同步配置命令失败: {e}")
            return "处理失败，请查看日志"
    
    def _get_sync_config_info(self) -> str:
        """获取同步配置信息"""
        response = "【消息同步配置】\n\n"
        
        response += "MC玩家自动同步到QQ:\n"
        response += f"  状态: {'已启用' if self.config['features']['mc_auto_sync_to_qq']['enabled'] else '已禁用'}\n"
        response += f"  群列表: {', '.join(map(str, self.config['features']['mc_auto_sync_to_qq']['group_ids'])) if self.config['features']['mc_auto_sync_to_qq']['group_ids'] else '无'}\n"
        response += f"  说明: MC玩家的聊天消息自动同步到指定QQ群\n\n"
        
        response += "MC玩家主动发送消息到QQ:\n"
        response += f"  状态: {'已启用' if self.config['features']['mc_manual_sync_to_qq']['enabled'] else '已禁用'}\n"
        response += f"  群列表: {', '.join(map(str, self.config['features']['mc_manual_sync_to_qq']['group_ids'])) if self.config['features']['mc_manual_sync_to_qq']['group_ids'] else '无'}\n"
        response += f"  说明: 使用 qq <消息> 命令发送消息到QQ群\n\n"
        
        response += "QQ用户通过命令发送消息到MC:\n"
        response += f"  状态: {'已启用' if self.config['features']['qq_manual_to_mc']['enabled'] else '已禁用'}\n"
        response += f"  群列表: {', '.join(map(str, self.config['features']['qq_manual_to_mc']['group_ids'])) if self.config['features']['qq_manual_to_mc']['group_ids'] else '无'}\n"
        response += f"  说明: 使用 mc <消息> 命令发送消息到MC服务器\n\n"
        
        response += "命令前缀:\n"
        response += f"  MC->QQ: {self.config['mc_commands']['qq_command_prefix']}\n"
        response += f"  QQ->MC: {self.config['qq_commands']['mc_command_prefix']}\n\n"
        
        response += "黑名单:\n"
        response += f"  玩家: {', '.join(self.config['blacklist']['players']) if self.config['blacklist']['players'] else '无'}\n"
        response += f"  用户: {', '.join(map(str, self.config['blacklist']['users'])) if self.config['blacklist']['users'] else '无'}\n"
        
        return response
    
    def get_plugin_help(self) -> str:
        """获取插件帮助"""
        return f"""
【MC-QQ消息同步】v{self.version}
作者: {self.author}
说明: {self.description}

功能:
• MC玩家聊天消息自动同步到QQ群
• MC玩家聊天消息主动发送到QQ群
• QQ群消息主动发送到MC服务器

MC游戏内命令:
  • {self.config['mc_commands']['qq_command_prefix']} <消息>
    功能: 主动发送消息到QQ群 (功能2)
    示例: qq 大家好

QQ群内命令:
  • {self.config['qq_commands']['mc_command_prefix']} <消息>
    功能: 通过命令发送消息到MC服务器 (功能3)
    说明: 需在QQ-MC绑定插件中绑定游戏ID
    示例: mc 你好

管理员命令 (QQ群内):
  • sync_config show
    功能: 查看当前同步配置
  • sync_config enable <功能>
    用法: sync_config enable <auto_mc|manual_mc|manual_qq>
  • sync_config disable <功能>
  • sync_config addgroup <功能> <群号>
  • sync_config removegroup <功能> <群号>
功能说明:
  • auto_mc    - MC玩家自动同步到QQ
  • manual_mc  - MC玩家主动发送消息到QQ
  • manual_qq  - QQ用户命令发送消息到MC
        """