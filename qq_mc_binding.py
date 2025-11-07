import asyncio
import json
import random
import time
from pathlib import Path
from typing import Dict, Optional, List, Any
from datetime import datetime
from plugin_manager import BotPlugin


class QQMCBindingPlugin(BotPlugin):
    """QQ 账号与 Minecraft 游戏 ID 绑定插件"""
    
    name = "QQ-MC 账号绑定"
    version = "1.0.0"
    author = "MSMP_QQBot"
    description = "允许玩家通过验证码将 QQ 号与 Minecraft 游戏 ID 绑定"
    
    def __init__(self, logger):
        super().__init__(logger)
        self.plugin_manager = None
        
        # 配置文件和数据的路径
        self.plugin_dir = Path("plugins/qq_mc_binding")
        self.config_file = self.plugin_dir / "config.json"
        self.data_file = self.plugin_dir / "binding_data.json"
        
        # 确保目录存在
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        
        # 加载配置
        self.config = self._load_config()
        
        # 运行时数据
        self.binding_data: Dict[str, List[Dict[str, Any]]] = {}  # {qq_id: [binding_info]}
        self.pending_verify: Dict[str, Dict[str, Any]] = {}       # {verify_code: {qq_id, expire_time}}
        self.mc_player_waiting: Dict[str, str] = {}               # {player_name: verify_code}
        
        # 日志轮询相关
        self._last_processed_log_index = 0
        self._log_check_task = None
        self._running = False
        self._server_running = False
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件，如果不存在则创建默认配置"""
        default_config = {
            'max_bindings_per_qq': 1,      # 每个 QQ 号最多可绑定的游戏 ID 数量
            'verify_timeout': 300,          # 验证码有效时间（秒）
            'verify_code_length': 6,        # 验证码长度
            'chat_message_pattern': r'.*\[Not Secure\]\s*<([^>]+)>\s*(.+)'  # 玩家聊天消息匹配模式
        }
        
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                
                # 合并配置，确保新字段会被添加
                merged_config = default_config.copy()
                merged_config.update(loaded_config)
                
                self.logger.info(f"已加载配置文件: {self.config_file}")
                return merged_config
            else:
                # 创建默认配置文件
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
            
            # 注册事件监听器
            plugin_manager.register_event_listener("server_started", self.on_server_started)
            plugin_manager.register_event_listener("server_stopping", self.on_server_stopping)
            
            # 加载持久化数据
            self._load_binding_data()
            
            # 注册 QQ 群命令
            plugin_manager.register_command(
                command_name="bind_qq",
                handler=self.handle_bind_command,
                names=["绑定", "bind"],
                description="绑定 QQ 号与游戏 ID",
                usage="绑定 - 获取验证码进行绑定"
            )
            
            # 注册查询绑定命令
            plugin_manager.register_command(
                command_name="query_binding",
                handler=self.handle_query_command,
                names=["查询绑定", "query"],
                description="查询已绑定的游戏 ID",
                usage="查询绑定 - 查看你的绑定信息"
            )
            
            # 注册解绑命令
            plugin_manager.register_command(
                command_name="unbind",
                handler=self.handle_unbind_command,
                names=["解绑", "unbind"],
                description="解除绑定",
                usage="解绑 <游戏ID> - 解除与该游戏ID的绑定"
            )
            
            # 注册管理命令
            plugin_manager.register_command(
                command_name="binding_admin",
                handler=self.handle_admin_command,
                names=["绑定管理", "binding_admin"],
                description="管理员命令：查看所有绑定",
                usage="绑定管理 list - 查看所有绑定\n绑定管理 delete <qq_id> <game_id> - 删除绑定",
                admin_only=True
            )
            
            # 已处理的日志时间戳缓存（避免重复处理）
            self._processed_log_timestamps = set()
            
            # 检查服务器是否已经在运行状态
            self._check_server_status()
            
            # 启动日志轮询任务
            self._running = True
            self._log_check_task = asyncio.create_task(self._log_polling_loop())
            
            self.logger.info("QQ-MC 账号绑定插件已加载")
            return True
        
        except Exception as e:
            self.logger.error(f"插件加载失败: {e}", exc_info=True)
            return False
    
    async def on_unload(self):
        """插件卸载"""
        try:
            # 停止日志轮询任务
            self._running = False
            self._server_running = False
            if self._log_check_task:
                self._log_check_task.cancel()
                try:
                    await self._log_check_task
                except asyncio.CancelledError:
                    pass
            
            # 保存数据
            self._save_binding_data()
            self.logger.info("QQ-MC 账号绑定插件已卸载")
        except Exception as e:
            self.logger.error(f"插件卸载失败: {e}", exc_info=True)
    
    async def on_config_reload(self, old_config: Dict, new_config: Dict):
        """配置重新加载"""
        try:
            if 'qq_mc_binding' in new_config:
                new_plugin_config = new_config['qq_mc_binding']
                self.config.update(new_plugin_config)
                self._save_config()
                self.logger.info("插件配置已更新")
        except Exception as e:
            self.logger.error(f"配置更新失败: {e}")
    
    def _check_server_status(self):
        """检查服务器运行状态"""
        try:
            # 使用插件管理器提供的API检查服务器状态
            if hasattr(self.plugin_manager, 'is_server_running'):
                self._server_running = self.plugin_manager.is_server_running()
            else:
                # 如果API不可用，记录警告但继续运行
                self._server_running = True
                
        except Exception as e:
            self.logger.error(f"检查服务器状态失败: {e}")

    async def on_server_started(self, *args, **kwargs):
        """服务器启动事件"""
        self.logger.info("服务器已启动，开始处理绑定验证")
        self._server_running = True
        
        # 服务器启动时，重置日志索引为0，从头开始处理
        self._last_processed_log_index = 0

    async def on_server_stopping(self, *args, **kwargs):
        """服务器停止事件"""
        self.logger.info("服务器正在停止，暂停绑定验证")
        self._server_running = False
    
    async def _log_polling_loop(self):
        """日志轮询循环"""
        while self._running:
            try:
                # 如果服务器未运行，尝试重新检查状态
                if not self._server_running:
                    self._check_server_status()
                    if not self._server_running:
                        self.logger.debug("服务器未运行，等待重试...")
                        await asyncio.sleep(5)
                        continue
                
                # 检查插件管理器是否可用
                if not self.plugin_manager:
                    await asyncio.sleep(5)
                    continue
                    
                await self._check_server_logs()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"日志轮询出错: {e}")
                await asyncio.sleep(5)
    
    async def _check_server_logs(self):
        """检查服务器日志中的玩家消息"""
        try:
            self._cleanup_expired_verify_codes()
            
            if not self.plugin_manager:
                return
                
            # 获取服务器日志
            logs = self.plugin_manager.get_server_logs(50)
            
            # 确保 logs 是列表
            if not isinstance(logs, list):
                return
                
            # 如果日志为空，直接返回
            if not logs:
                return
            
            # 处理新的日志行（从后往前处理，只处理最新的）
            new_logs_processed = 0
            for i in range(len(logs) - 1, -1, -1):  # 从最新到最旧处理
                log_line = logs[i]
                if isinstance(log_line, str):
                    # 生成日志的唯一标识（使用时间戳）
                    log_hash = self._get_log_hash(log_line)
                    
                    # 如果已经处理过，跳过
                    if log_hash in self._processed_log_timestamps:
                        continue
                    
                    # 处理日志
                    processed = await self._process_log_line(log_line)
                    if processed:
                        new_logs_processed += 1
                        # 记录已处理的日志
                        self._processed_log_timestamps.add(log_hash)
                        self.logger.debug(f"成功处理日志: {log_line}")
            
            # 清理过期的日志缓存（避免内存无限增长）
            self._cleanup_processed_logs_cache()
            
        except Exception as e:
            self.logger.error(f"检查服务器日志失败: {e}")

    def _get_log_hash(self, log_line: str) -> str:
        """生成日志的唯一标识"""
        try:
            import hashlib
            # 使用MD5生成日志的哈希值作为唯一标识
            return hashlib.md5(log_line.encode('utf-8')).hexdigest()
        except:
            # 如果哈希失败，使用原始字符串（简单场景下也够用）
            return log_line

    def _cleanup_processed_logs_cache(self):
        """清理已处理日志缓存，避免内存无限增长"""
        try:
            # 如果缓存太大，清理一部分（保留最近200条）
            if len(self._processed_log_timestamps) > 200:
                # 转换为列表，删除前一半
                timestamps_list = list(self._processed_log_timestamps)
                remove_count = len(timestamps_list) // 2
                for i in range(remove_count):
                    self._processed_log_timestamps.discard(timestamps_list[i])
                
                self.logger.debug(f"清理已处理日志缓存，移除 {remove_count} 条记录")
        except Exception as e:
            self.logger.error(f"清理日志缓存失败: {e}")

    async def _process_log_line(self, log_line: str) -> bool:
        """处理单条日志行，返回是否处理了消息"""
        try:
            import re
            
            # 首先检查日志行是否包含聊天消息的关键特征
            if '[Not Secure]' not in log_line:
                return False
                
            if '<' not in log_line or '>' not in log_line:
                return False
                
            pattern = self.config['chat_message_pattern']
            match = re.search(pattern, log_line)
            
            if not match:
                return False
            
            player_name = match.group(1).strip()
            message = match.group(2).strip()
            
            # 直接检查消息是否为验证码（纯数字）
            verify_code = message.strip()
            
            # 检查是否为纯数字验证码
            if not verify_code or not verify_code.isdigit():
                return False
            
            # 检查验证码长度
            expected_length = self.config['verify_code_length']
            if len(verify_code) != expected_length:
                return False
            
            # 处理验证码
            await self._process_verify_code(verify_code, player_name)
            return True
            
        except Exception as e:
            self.logger.error(f"处理日志行失败: {e}")
            return False
    
    async def _process_verify_code(self, verify_code: str, player_name: str):
        """处理验证码"""
        try:
            # 检查验证码是否存在且有效
            if verify_code not in self.pending_verify:
                self.logger.warning(f"玩家 {player_name} 使用无效验证码: {verify_code}")
                return
            
            verify_info = self.pending_verify[verify_code]
            
            # 检查验证码是否过期
            if time.time() > verify_info['expire_time']:
                del self.pending_verify[verify_code]
                self.logger.warning(f"玩家 {player_name} 使用过期验证码: {verify_code}")
                return
            
            # 检查验证码是否已经被使用（防止多人同时使用同一个验证码）
            if verify_info.get('used', False):
                self.logger.warning(f"玩家 {player_name} 使用已被使用的验证码: {verify_code}")
                return
            
            # 标记验证码为已使用（防止其他人再次使用）
            verify_info['used'] = True
            qq_id = verify_info['qq_id']
            
            # 检查该游戏ID是否已经被其他QQ号绑定
            existing_binding = self._find_binding_by_game_id(player_name)
            if existing_binding:
                # 如果游戏ID已经被绑定，检查是否是同一个QQ号
                if existing_binding['qq_id'] != qq_id:
                    self.logger.warning(f"游戏ID {player_name} 已被QQ {existing_binding['qq_id']} 绑定，无法再次绑定")
                    # 发送错误消息给玩家
                    await self._send_binding_failed_notification(player_name, f"该游戏ID已被其他QQ号绑定")
                    del self.pending_verify[verify_code]
                    return
                else:
                    # 同一个QQ号重复绑定同一个游戏ID
                    del self.pending_verify[verify_code]
                    self.logger.warning(f"QQ {qq_id} 尝试重复绑定: {player_name}")
                    return
            
            # 创建绑定信息
            binding_info = {
                'game_id': player_name,
                'qq_id': qq_id,
                'bind_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # 保存绑定信息
            if qq_id not in self.binding_data:
                self.binding_data[qq_id] = []
            
            self.binding_data[qq_id].append(binding_info)
            self._save_binding_data()
            
            # 清理验证码
            del self.pending_verify[verify_code]
            if player_name in self.mc_player_waiting:
                del self.mc_player_waiting[player_name]
            
            self.logger.info(f"绑定成功: QQ {qq_id} -> {player_name}")
            
            # 发送成功消息到游戏
            await self._send_binding_success_notification(player_name, qq_id)
            
        except Exception as e:
            self.logger.error(f"处理验证码失败: {e}")

    def _find_binding_by_game_id(self, game_id: str) -> Optional[Dict[str, Any]]:
        """根据游戏ID查找绑定信息"""
        try:
            for qq_id, bindings in self.binding_data.items():
                for binding in bindings:
                    if binding['game_id'] == game_id:
                        return binding
            return None
        except Exception as e:
            self.logger.error(f"查找绑定信息失败: {e}")
            return None

    async def _send_binding_failed_notification(self, player_name: str, reason: str):
        """发送绑定失败通知"""
        try:
            if self.plugin_manager and hasattr(self.plugin_manager, 'qq_server'):
                # 通过RCON发送游戏内消息
                if (hasattr(self.plugin_manager.qq_server, 'rcon_client') and 
                    self.plugin_manager.qq_server.rcon_client and 
                    self.plugin_manager.qq_server.rcon_client.is_connected()):
                    
                    error_msg = f"tell {player_name} 绑定失败: {reason}"
                    self.plugin_manager.qq_server.rcon_client.execute_command(error_msg)
                    self.logger.info(f"已发送游戏内绑定失败消息给 {player_name}")
                    
        except Exception as e:
            self.logger.error(f"发送绑定失败通知失败: {e}")
    
    async def _send_binding_success_notification(self, player_name: str, qq_id: str):
        """发送绑定成功通知"""
        try:
            if self.plugin_manager and hasattr(self.plugin_manager, 'qq_server'):
                # 通过RCON发送游戏内消息
                if (hasattr(self.plugin_manager.qq_server, 'rcon_client') and 
                    self.plugin_manager.qq_server.rcon_client and 
                    self.plugin_manager.qq_server.rcon_client.is_connected()):
                    
                    success_msg = f"tell {player_name} 绑定成功！QQ: {qq_id}"
                    self.plugin_manager.qq_server.rcon_client.execute_command(success_msg)
                    self.logger.info(f"已发送游戏内绑定成功消息给 {player_name}")
                
                # 发送QQ消息通知
                if (hasattr(self.plugin_manager.qq_server, 'current_connection') and 
                    self.plugin_manager.qq_server.current_connection and 
                    not self.plugin_manager.qq_server.current_connection.closed):
                    
                    qq_msg = f"玩家 {player_name} 绑定成功！(QQ: {qq_id})"
                    for group_id in self.plugin_manager.qq_server.allowed_groups:
                        await self.plugin_manager.qq_server.send_group_message(
                            self.plugin_manager.qq_server.current_connection,
                            group_id,
                            qq_msg
                        )
                    self.logger.info(f"已发送QQ绑定成功消息")
                
        except Exception as e:
            self.logger.error(f"发送绑定成功通知失败: {e}")
    
    def _load_binding_data(self):
        """从文件加载持久化数据"""
        try:
            if self.data_file.exists():
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    self.binding_data = json.load(f)
                self.logger.info(f"已加载绑定数据，共 {len(self.binding_data)} 个 QQ 号")
            else:
                self.binding_data = {}
        except Exception as e:
            self.logger.error(f"加载绑定数据失败: {e}")
            self.binding_data = {}
    
    def _save_binding_data(self):
        """保存数据到文件"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.binding_data, f, ensure_ascii=False, indent=2)
            self.logger.debug("绑定数据已保存")
        except Exception as e:
            self.logger.error(f"保存绑定数据失败: {e}")
    
    def _generate_verify_code(self) -> str:
        """生成验证码"""
        return ''.join(str(random.randint(0, 9)) for _ in range(self.config['verify_code_length']))
    
    def _cleanup_expired_verify_codes(self):
        """清理过期的验证码"""
        current_time = time.time()
        expired_codes = [
            code for code, info in self.pending_verify.items()
            if current_time > info['expire_time']
        ]
        
        for code in expired_codes:
            qq_id = self.pending_verify[code].get('qq_id')
            del self.pending_verify[code]
            
            # 清理对应的玩家等待信息
            players_to_remove = [
                player for player, verify_code in self.mc_player_waiting.items()
                if verify_code == code
            ]
            for player in players_to_remove:
                del self.mc_player_waiting[player]
    
    async def handle_bind_command(self, user_id: int, group_id: int,
                                  command_text: str, **kwargs) -> Optional[str]:
        """处理绑定命令"""
        try:
            qq_id = str(user_id)
            
            # 检查当前绑定数量
            current_bindings = len(self.binding_data.get(qq_id, []))
            max_bindings = self.config['max_bindings_per_qq']
            
            if current_bindings >= max_bindings:
                return (
                    f"[CQ:at,qq={user_id}] 绑定失败\n"
                    f"你已绑定 {current_bindings} 个游戏 ID，"
                    f"最多只能绑定 {max_bindings} 个\n"
                    f"(输入 查询绑定 查看已绑定的 ID)"
                )
            
            # 清理该QQ号之前的未使用验证码
            self._cleanup_previous_verify_codes(qq_id)
            
            # 生成验证码
            verify_code = self._get_unique_verify_code()
            expire_time = time.time() + self.config['verify_timeout']
            
            self.pending_verify[verify_code] = {
                'qq_id': qq_id,
                'expire_time': expire_time,
                'created_at': datetime.now().isoformat(),
                'used': False  # 标记为未使用
            }
            
            self.logger.info(f"为 QQ {qq_id} 生成验证码: {verify_code}")
            
            return (
                f"[CQ:at,qq={user_id}] 有效期: {self.config['verify_timeout']} 秒\n"
                f"验证码: {verify_code}\n"
                f"请在游戏内直接发送验证码"
            )
        
        except Exception as e:
            self.logger.error(f"处理绑定命令失败: {e}")
            return f"[CQ:at,qq={user_id}] 处理失败，请稍后重试"
    
    def _cleanup_previous_verify_codes(self, qq_id: str):
        """清理该QQ号之前的未使用验证码"""
        try:
            codes_to_remove = []
            for code, info in self.pending_verify.items():
                if info['qq_id'] == qq_id and not info.get('used', False):
                    codes_to_remove.append(code)
            
            for code in codes_to_remove:
                del self.pending_verify[code]
                self.logger.info(f"清理QQ {qq_id} 的旧验证码: {code}")
                
        except Exception as e:
            self.logger.error(f"清理旧验证码失败: {e}")

    def _get_unique_verify_code(self) -> str:
        """生成唯一的验证码"""
        while True:
            code = self._generate_verify_code()
            # 检查验证码是否已存在且未过期
            if code not in self.pending_verify:
                return code
            else:
                # 如果验证码已存在但已过期，可以重用
                verify_info = self.pending_verify[code]
                if time.time() > verify_info['expire_time']:
                    del self.pending_verify[code]
                    return code

    async def handle_query_command(self, user_id: int, group_id: int,
                               command_text: str, **kwargs) -> Optional[str]:
        """处理查询绑定命令"""
        try:
            qq_id = str(user_id)
            
            if qq_id not in self.binding_data or not self.binding_data[qq_id]:
                return (
                    f"[CQ:at,qq={user_id}] 你还没有绑定任何游戏 ID\n"
                    f"输入 绑定 开始绑定"
                )
            
            bindings = self.binding_data[qq_id]
            response = f"[CQ:at,qq={user_id}] 你有 {len(bindings)} 个绑定:\n\n"
            
            for i, binding in enumerate(bindings, 1):
                game_id = binding.get('game_id', '未知')
                bind_time = binding.get('bind_time', '未知')
                response += (
                    f"{i}. 游戏 ID: {game_id}\n"
                    f"   绑定时间: {bind_time}\n\n"
                )
            
            response += f"输入 解绑 <游戏ID> 可以解除绑定"
            return response
        
        except Exception as e:
            self.logger.error(f"处理查询命令失败: {e}")
            return f"[CQ:at,qq={user_id}] 处理失败，请稍后重试"

    async def handle_unbind_command(self, user_id: int, group_id: int,
                                   command_text: str, **kwargs) -> Optional[str]:
        """处理解绑命令"""
        try:
            qq_id = str(user_id)
            
            if not command_text or not command_text.strip():
                return (
                    f"[CQ:at,qq={user_id}] 请指定要解绑的游戏 ID\n"
                    f"用法: 解绑 <游戏ID>\n"
                    f"例如: 解绑 Steve"
                )
            
            game_id = command_text.strip()
            
            if qq_id not in self.binding_data:
                return f"[CQ:at,qq={user_id}] 你没有任何绑定信息"
            
            bindings = self.binding_data[qq_id]
            for i, binding in enumerate(bindings):
                if binding['game_id'] == game_id:
                    removed = bindings.pop(i)
                    
                    if not bindings:
                        del self.binding_data[qq_id]
                    
                    self._save_binding_data()
                    self.logger.info(f"QQ {qq_id} 已解绑游戏 ID: {game_id}")
                    
                    return (
                        f"[CQ:at,qq={user_id}] 解绑成功\n"
                        f"游戏 ID: {game_id}\n"
                        f"解绑时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
            
            return f"[CQ:at,qq={user_id}] 找不到绑定的游戏 ID: {game_id}"
        
        except Exception as e:
            self.logger.error(f"处理解绑命令失败: {e}")
            return f"[CQ:at,qq={user_id}] 处理失败，请稍后重试"
    
    async def handle_admin_command(self, user_id: int, group_id: int,
                                  command_text: str, **kwargs) -> Optional[str]:
        """处理管理员命令"""
        try:
            args = command_text.strip().split()
            
            if not args:
                return (
                    f"绑定管理命令\n\n"
                    f"• 绑定管理 list - 查看所有绑定\n"
                    f"• 绑定管理 delete <qq_id> <game_id> - 删除绑定"
                )
            
            command = args[0].lower()
            
            if command == 'list':
                if not self.binding_data:
                    return "没有任何绑定信息"
                
                response = f"总绑定数: {sum(len(v) for v in self.binding_data.values())}\n"
                response += f"涉及 QQ: {len(self.binding_data)} 个\n\n"
                
                for qq_id, bindings in self.binding_data.items():
                    response += f"QQ {qq_id}:\n"
                    for binding in bindings:
                        response += f"  • {binding['game_id']} ({binding['bind_time']})\n"
                
                return response
            
            elif command == 'delete':
                if len(args) < 3:
                    return "参数不足: 绑定管理 delete <qq_id> <game_id>"
                
                qq_id = args[1]
                game_id = args[2]
                
                if qq_id not in self.binding_data:
                    return f"找不到 QQ {qq_id} 的绑定信息"
                
                bindings = self.binding_data[qq_id]
                for i, binding in enumerate(bindings):
                    if binding['game_id'] == game_id:
                        bindings.pop(i)
                        if not bindings:
                            del self.binding_data[qq_id]
                        
                        self._save_binding_data()
                        return f"已删除 QQ {qq_id} 的绑定: {game_id}"
                
                return f"找不到该绑定: QQ {qq_id} -> {game_id}"
            
            else:
                return f"未知命令: {command}"
        
        except Exception as e:
            self.logger.error(f"处理管理员命令失败: {e}")
            return "处理失败，请稍后重试"

    def get_plugin_help(self) -> str:
        """获取插件帮助信息"""
        return f"""
【QQ-MC 账号绑定】v{self.version}
作者: {self.author}
说明: {self.description}

命令列表:

绑定账号
• 绑定 或 bind
  获取验证码，然后在游戏内直接发送验证码来完成绑定
  每个 QQ 最多可绑定 {self.config['max_bindings_per_qq']} 个游戏 ID

查询绑定
• 查询绑定 或 query
  查看你已绑定的所有游戏 ID 和绑定时间

解除绑定
• 解绑 <游戏ID>

管理员命令
• 绑定管理 list - 查看所有绑定信息
• 绑定管理 delete <qq_id> <game_id> - 删除指定绑定
        """