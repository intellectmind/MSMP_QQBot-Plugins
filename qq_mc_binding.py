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
    version = "2.0.0"
    author = "MSMP_QQBot"
    description = "允许玩家通过验证码将 QQ 号与 Minecraft 游戏 ID 绑定"
    DEFAULT_CONFIG = {
        'max_bindings_per_qq': 1,
        'verify_timeout': 300,
        'verify_code_length': 6,
        'chat_message_pattern': r'.*(?:\[Not Secure\]\s*)?<([^>]+)>\s*(.+)'
    }
    FLEXIBLE_CHAT_MESSAGE_PATTERN = r'.*(?:\[Not Secure\]\s*)?<([^>]+)>\s*(.+)'
    
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
        self.pending_verify: Dict[str, Dict[str, Any]] = {}       # {server_key::verify_code: {qq_id, expire_time}}
        self.mc_player_waiting: Dict[str, str] = {}               # {server_key::player_name: verify_code}
        
        # 日志轮询相关
        self._last_processed_log_index = 0
        self._log_check_task = None
        self._running = False
        self._server_running = False
        self._processed_log_timestamps = set()
        self._seeded_log_sources = set()
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件，如果不存在则创建默认配置"""
        default_config = self.DEFAULT_CONFIG.copy()
        
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
                self._server_running = bool(self._running_target_servers())
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
                    
                for target_server in self._running_target_servers():
                    await self._check_server_logs(target_server)
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"日志轮询出错: {e}")
                await asyncio.sleep(5)
    
    async def _check_server_logs(self, target_server: Optional[Dict[str, Any]] = None):
        """检查服务器日志中的玩家消息"""
        try:
            self._cleanup_expired_verify_codes()
            
            if not self.plugin_manager:
                return
                
            if hasattr(self.plugin_manager, 'get_incremental_server_logs'):
                logs = self.plugin_manager.get_incremental_server_logs(
                    "qq_mc_binding",
                    1000,
                    target_server
                )
            else:
                logs = self.plugin_manager.get_server_logs(200, target_server)
            
            # 确保 logs 是列表
            if not isinstance(logs, list):
                return
                
            # 如果日志为空，直接返回
            if not logs:
                return

            server_key = self._server_key(target_server)
            if self._should_seed_log_source(target_server, server_key):
                for log_line in logs:
                    if isinstance(log_line, str):
                        self._processed_log_timestamps.add(self._get_log_hash(f"{server_key}::{log_line}"))
                self._seeded_log_sources.add(server_key)
                return
            
            # 处理新的日志行（从后往前处理，只处理最新的）
            new_logs_processed = 0
            for i in range(len(logs) - 1, -1, -1):  # 从最新到最旧处理
                log_line = logs[i]
                if isinstance(log_line, str):
                    # 生成日志的唯一标识（使用时间戳）
                    log_hash = self._get_log_hash(f"{server_key}::{log_line}")
                    
                    # 如果已经处理过，跳过
                    if log_hash in self._processed_log_timestamps:
                        continue
                    
                    # 处理日志
                    processed = await self._process_log_line(log_line, target_server)
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

    def _running_target_servers(self) -> List[Dict[str, Any]]:
        """返回当前需要轮询日志的运行中服务器列表。"""
        if self.plugin_manager and hasattr(self.plugin_manager, 'get_running_server_configs'):
            servers = self.plugin_manager.get_running_server_configs()
            if servers:
                return servers

        active_server = self._active_target_server()
        if self.plugin_manager and hasattr(self.plugin_manager, 'is_server_running'):
            if active_server and self.plugin_manager.is_server_running(active_server):
                return [active_server]
            if self.plugin_manager.is_server_running():
                return [active_server]
        return [active_server] if active_server else []

    def _cleanup_processed_logs_cache(self):
        """清理已处理日志缓存，避免内存无限增长"""
        try:
            # 多服务器 latest.log 基线会占用较多 hash，缓存过小会导致旧日志重复处理。
            if len(self._processed_log_timestamps) > 5000:
                # 转换为列表，删除前一半
                timestamps_list = list(self._processed_log_timestamps)
                remove_count = len(timestamps_list) // 2
                for i in range(remove_count):
                    self._processed_log_timestamps.discard(timestamps_list[i])
                
                self.logger.debug(f"清理已处理日志缓存，移除 {remove_count} 条记录")
        except Exception as e:
            self.logger.error(f"清理日志缓存失败: {e}")

    def _should_seed_log_source(self, target_server: Optional[Dict[str, Any]], server_key: str) -> bool:
        if server_key in self._seeded_log_sources:
            return False
        if not self.plugin_manager or not hasattr(self.plugin_manager, 'is_server_running'):
            return False
        try:
            return not self.plugin_manager.is_server_running(target_server)
        except Exception:
            return False

    async def _process_log_line(self, log_line: str, target_server: Optional[Dict[str, Any]] = None) -> bool:
        """处理单条日志行，返回是否处理了消息"""
        try:
            import re
            
            if '<' not in log_line or '>' not in log_line:
                return False
                
            active_config = self._config_for_server(target_server)
            pattern = active_config.get('chat_message_pattern') or self.config['chat_message_pattern']
            match = None
            for candidate in dict.fromkeys([pattern, self.FLEXIBLE_CHAT_MESSAGE_PATTERN]):
                match = re.search(candidate, log_line)
                if match:
                    break
            
            if not match:
                return False
            
            player_name = match.group(1).strip()
            message = match.group(2).strip()
            
            # 直接检查消息是否为验证码（纯数字）
            verify_code = message.strip()
            
            # 检查是否为纯数字验证码
            if not verify_code or not verify_code.isdigit():
                return False
            
            # 处理验证码
            await self._process_verify_code(verify_code, player_name, target_server)
            return True
            
        except Exception as e:
            self.logger.error(f"处理日志行失败: {e}")
            return False
    
    async def _process_verify_code(
        self,
        verify_code: str,
        player_name: str,
        target_server: Optional[Dict[str, Any]] = None
    ):
        """处理验证码"""
        try:
            verify_key = self._resolve_verify_key(verify_code, target_server)
            # 检查验证码是否存在且有效
            if not verify_key:
                self.logger.warning(f"玩家 {player_name} 使用无效验证码: {verify_code}")
                return
            
            verify_info = self.pending_verify[verify_key]
            
            # 检查验证码是否过期
            if time.time() > verify_info['expire_time']:
                del self.pending_verify[verify_key]
                self.logger.warning(f"玩家 {player_name} 使用过期验证码: {verify_code}")
                return
            
            # 检查验证码是否已经被使用（防止多人同时使用同一个验证码）
            if verify_info.get('used', False):
                self.logger.warning(f"玩家 {player_name} 使用已被使用的验证码: {verify_code}")
                return
            
            # 标记验证码为已使用（防止其他人再次使用）
            verify_info['used'] = True
            qq_id = verify_info['qq_id']
            server_key = verify_info.get('server_key') or 'default'
            target_server = verify_info.get('target_server') or self._target_server_from_key(server_key)
            
            # 检查该游戏ID是否已经被其他QQ号绑定
            existing_binding = self._find_binding_by_game_id(player_name, server_key=server_key)
            if existing_binding:
                # 如果游戏ID已经被绑定，检查是否是同一个QQ号
                if existing_binding['qq_id'] != qq_id:
                    self.logger.warning(f"游戏ID {player_name} 已被QQ {existing_binding['qq_id']} 绑定，无法再次绑定")
                    # 发送错误消息给玩家
                    await self._send_binding_failed_notification(player_name, f"该游戏ID已被其他QQ号绑定", target_server)
                    del self.pending_verify[verify_key]
                    return
                else:
                    # 同一个QQ号重复绑定同一个游戏ID
                    del self.pending_verify[verify_key]
                    self.logger.warning(f"QQ {qq_id} 尝试重复绑定: {player_name}")
                    return
            
            # 创建绑定信息
            binding_info = {
                'game_id': player_name,
                'qq_id': qq_id,
                'server_key': server_key,
                'server_name': verify_info.get('server_name') or self._server_name(target_server),
                'bind_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # 保存绑定信息
            if qq_id not in self.binding_data:
                self.binding_data[qq_id] = []
            
            self.binding_data[qq_id].append(binding_info)
            self._save_binding_data()
            
            # 清理验证码
            del self.pending_verify[verify_key]
            waiting_key = self._waiting_key(server_key, player_name)
            if waiting_key in self.mc_player_waiting:
                del self.mc_player_waiting[waiting_key]
            
            self.logger.info(f"绑定成功: QQ {qq_id} -> {player_name}")
            
            # 发送成功消息到游戏
            await self._send_binding_success_notification(player_name, qq_id, target_server)
            
        except Exception as e:
            self.logger.error(f"处理验证码失败: {e}")

    def _find_binding_by_game_id(self, game_id: str, target_server: Optional[Dict[str, Any]] = None,
                                 server_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """根据游戏ID查找绑定信息"""
        try:
            target_key = server_key or self._server_key(target_server)
            for qq_id, bindings in self.binding_data.items():
                for binding in bindings:
                    if self._binding_server_key(binding) == target_key and binding.get('game_id') == game_id:
                        result = dict(binding)
                        result.setdefault('qq_id', qq_id)
                        return result
            return None
        except Exception as e:
            self.logger.error(f"查找绑定信息失败: {e}")
            return None

    async def _send_binding_failed_notification(self, player_name: str, reason: str,
                                                target_server: Optional[Dict[str, Any]] = None):
        """发送绑定失败通知"""
        try:
            if self.plugin_manager and hasattr(self.plugin_manager, 'qq_server'):
                await self._execute_mc_command(
                    f"tell {player_name} 绑定失败: {reason}",
                    target_server
                )
                self.logger.info(f"已发送游戏内绑定失败消息给 {player_name}")
                    
        except Exception as e:
            self.logger.error(f"发送绑定失败通知失败: {e}")
    
    async def _send_binding_success_notification(self, player_name: str, qq_id: str,
                                                 target_server: Optional[Dict[str, Any]] = None):
        """发送绑定成功通知"""
        try:
            if self.plugin_manager and hasattr(self.plugin_manager, 'qq_server'):
                await self._execute_mc_command(
                    f"tell {player_name} 绑定成功！QQ: {qq_id}",
                    target_server
                )
                self.logger.info(f"已发送游戏内绑定成功消息给 {player_name}")
                
                # 发送QQ消息通知
                current_connection = getattr(self.plugin_manager.qq_server, 'current_connection', None)
                connection_open = (
                    hasattr(self.plugin_manager.qq_server, '_websocket_open') and
                    self.plugin_manager.qq_server._websocket_open(current_connection)
                )
                if connection_open:
                    
                    qq_msg = f"玩家 {player_name} 绑定成功！(QQ: {qq_id})"
                    active_server = target_server or getattr(self.plugin_manager.qq_server, 'active_server_config', None) or {}
                    qq_config = active_server.get('qq') or {}
                    target_groups = qq_config.get('groups') or self.plugin_manager.qq_server.allowed_groups
                    for group_id in target_groups:
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
        loaded_data: Dict[str, List[Dict[str, Any]]] = {}
        try:
            server_files = self._server_binding_files()
            if self.data_file.exists():
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    loaded_data = self._merge_binding_data(loaded_data, json.load(f))
                self.logger.info(f"已读取旧绑定数据文件: {self.data_file}")

            for server_key, data_file in server_files.items():
                if not data_file.exists():
                    continue
                with open(data_file, 'r', encoding='utf-8') as f:
                    loaded_data = self._merge_binding_data(loaded_data, json.load(f), server_key=server_key)

            self.binding_data = loaded_data
            self.logger.info(f"已加载绑定数据，共 {len(self.binding_data)} 个 QQ 号")
        except Exception as e:
            self.logger.error(f"加载绑定数据失败: {e}")
            self.binding_data = {}
    
    def _save_binding_data(self):
        """保存数据到文件"""
        try:
            grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
            for qq_id, bindings in self.binding_data.items():
                for binding in bindings:
                    server_key = self._binding_server_key(binding)
                    grouped.setdefault(server_key, {}).setdefault(str(qq_id), []).append(binding)

            for server_key in sorted(set(grouped.keys()) | set(self._server_binding_files().keys())):
                data_file = self._server_binding_file(server_key)
                with open(data_file, 'w', encoding='utf-8') as f:
                    json.dump(grouped.get(server_key, {}), f, ensure_ascii=False, indent=2)
            self.logger.debug("绑定数据已保存")
        except Exception as e:
            self.logger.error(f"保存绑定数据失败: {e}")

    def _merge_binding_data(self, current: Dict[str, List[Dict[str, Any]]], incoming,
                            server_key: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """合并绑定数据；服务器目录中的旧数据如果缺少 server_key，则补上目录对应 server_key。"""
        if not isinstance(incoming, dict):
            return current
        seen = {
            (str(qq_id), str(binding.get('game_id')), self._binding_server_key(binding))
            for qq_id, bindings in current.items()
            for binding in bindings
            if isinstance(binding, dict)
        }
        for qq_id, bindings in incoming.items():
            if not isinstance(bindings, list):
                continue
            target_list = current.setdefault(str(qq_id), [])
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                item = dict(binding)
                if server_key and not item.get('server_key'):
                    item['server_key'] = server_key
                key = (str(qq_id), str(item.get('game_id')), self._binding_server_key(item))
                if key in seen:
                    continue
                seen.add(key)
                target_list.append(item)
        return current

    def _server_binding_files(self) -> Dict[str, Path]:
        files = {}
        if not self.plugin_manager or not hasattr(self.plugin_manager, 'get_configured_servers'):
            return files
        for server in self.plugin_manager.get_configured_servers():
            server_key = self._server_key(server)
            files[server_key] = self._server_binding_file(server_key)
        return files

    def _server_binding_file(self, server_key: str) -> Path:
        if self.plugin_manager and hasattr(self.plugin_manager, 'get_plugin_server_file_by_key'):
            return self.plugin_manager.get_plugin_server_file_by_key(
                "qq_mc_binding", server_key, "binding_data.json"
            )
        fallback = self.plugin_dir / "servers" / self._safe_path_name(server_key)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "binding_data.json"

    def _safe_path_name(self, value: str) -> str:
        text = str(value or "default").replace("\\", "/").strip()
        if "/" in text:
            text = Path(text).stem
        safe = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in text)
        return safe.strip("._") or "default"
    
    def _generate_verify_code(self, config: Optional[Dict[str, Any]] = None) -> str:
        """生成验证码"""
        active_config = config or self.config
        return ''.join(str(random.randint(0, 9)) for _ in range(active_config['verify_code_length']))

    def _active_target_server(self) -> Dict[str, Any]:
        """获取当前活跃服务器配置。"""
        if self.plugin_manager and getattr(self.plugin_manager, 'qq_server', None):
            return getattr(self.plugin_manager.qq_server, 'active_server_config', None) or {}
        return {}

    def _server_key(self, target_server: Optional[Dict[str, Any]] = None) -> str:
        """获取服务器稳定标识，和主命令路由保持一致。"""
        server = target_server or self._active_target_server()
        return str((server or {}).get('_config_file') or (server or {}).get('name') or 'default')

    def _server_name(self, target_server: Optional[Dict[str, Any]] = None) -> str:
        server = target_server or self._active_target_server()
        return str((server or {}).get('name') or self._server_key(server))

    def _binding_server_key(self, binding: Dict[str, Any]) -> str:
        return str((binding or {}).get('server_key') or (binding or {}).get('server') or self._legacy_server_key())

    def _legacy_server_key(self) -> str:
        """历史绑定没有服务器字段时，运行时归属到第一个服务器，避免跨服同时可见。"""
        qq_server = getattr(self.plugin_manager, 'qq_server', None) if self.plugin_manager else None
        config_manager = getattr(qq_server, 'config_manager', None)
        if config_manager and hasattr(config_manager, 'get_servers'):
            servers = config_manager.get_servers()
            if servers:
                return self._server_key(servers[0])
        active_server = getattr(qq_server, 'active_server_config', None) if qq_server else None
        if active_server:
            return self._server_key(active_server)
        return 'default'

    def _bindings_for_qq(self, qq_id: str, target_server: Optional[Dict[str, Any]] = None,
                         server_key: Optional[str] = None) -> List[Dict[str, Any]]:
        target_key = server_key or self._server_key(target_server)
        return [
            binding for binding in self.binding_data.get(qq_id, [])
            if self._binding_server_key(binding) == target_key
        ]

    def _waiting_key(self, server_key: str, player_name: str) -> str:
        return f"{server_key}::{player_name}"

    def _verify_storage_key(self, server_key: str, verify_code: str) -> str:
        return f"{server_key or 'default'}::{verify_code}"

    def _verify_display_code(self, storage_key: str) -> str:
        text = str(storage_key)
        return text.rsplit("::", 1)[-1] if "::" in text else text

    def _resolve_verify_key(self, verify_code: str, target_server: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """按服务器优先解析验证码，避免多服务器相同验证码串服。"""
        explicit_target = bool(target_server)
        preferred_key = self._server_key(target_server or self._active_target_server())
        preferred = self._verify_storage_key(preferred_key, verify_code)
        preferred_info = self.pending_verify.get(preferred)
        if (
            preferred_info
            and not preferred_info.get('used', False)
            and time.time() <= preferred_info.get('expire_time', 0)
        ):
            return preferred

        if explicit_target:
            return None

        matches = [
            key for key, info in self.pending_verify.items()
            if self._verify_display_code(key) == str(verify_code)
            and not info.get('used', False)
            and time.time() <= info.get('expire_time', 0)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            self.logger.warning(f"验证码 {verify_code} 同时存在于多个服务器，无法自动判定")
        return None

    def _target_server_from_key(self, server_key: str) -> Dict[str, Any]:
        qq_server = getattr(self.plugin_manager, 'qq_server', None) if self.plugin_manager else None
        config_manager = getattr(qq_server, 'config_manager', None)
        if config_manager and hasattr(config_manager, 'get_servers'):
            for server in config_manager.get_servers():
                if self._server_key(server) == server_key:
                    return server
        active_server = getattr(qq_server, 'active_server_config', None) if qq_server else None
        if active_server and self._server_key(active_server) == server_key:
            return active_server
        return {}

    def _config_for_server(self, target_server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """读取目标服务器的独立插件配置，找不到则回退插件根配置。"""
        config = dict(self.DEFAULT_CONFIG)
        config.update(self.config or {})
        if self.plugin_manager and hasattr(self.plugin_manager, 'get_plugin_server_file'):
            path = self.plugin_manager.get_plugin_server_file(
                "qq_mc_binding", "config.json", target_server or self._active_target_server(), create_parent=False
            )
            try:
                if path.exists():
                    with open(path, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        config.update(loaded)
            except Exception as e:
                self.logger.error(f"读取服务器插件配置失败 {path}: {e}")
        return config

    async def _execute_mc_command(self, command: str, target_server: Optional[Dict[str, Any]] = None):
        qq_server = getattr(self.plugin_manager, 'qq_server', None) if self.plugin_manager else None
        if not qq_server:
            return None
        explicit_target = bool(target_server)
        executor = getattr(qq_server, '_execute_server_command', None)
        if callable(executor):
            return await executor(command, target_server or self._active_target_server())
        if explicit_target:
            self.logger.warning("目标服务器缺少按服执行器，拒绝回退到全局RCON")
            return None
        rcon = getattr(qq_server, 'rcon_client', None)
        if rcon and hasattr(rcon, "run_connected"):
            connected, result = await asyncio.to_thread(
                rcon.run_connected,
                lambda client: client.execute_command(command)
            )
            return result if connected else None
        if rcon and rcon.is_connected():
            return await asyncio.to_thread(rcon.execute_command, command)
        return None
    
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
            display_code = self._verify_display_code(code)
            players_to_remove = [
                player for player, verify_code in self.mc_player_waiting.items()
                if verify_code == code or verify_code == display_code
            ]
            for player in players_to_remove:
                del self.mc_player_waiting[player]
    
    async def handle_bind_command(self, user_id: int, group_id: int,
                                  command_text: str, **kwargs) -> Optional[str]:
        """处理绑定命令"""
        try:
            qq_id = str(user_id)
            target_server = kwargs.get('target_server') or self._active_target_server()
            server_key = self._server_key(target_server)
            server_name = self._server_name(target_server)
            config = self._config_for_server(target_server)
            
            # 检查当前绑定数量
            current_bindings = len(self._bindings_for_qq(qq_id, server_key=server_key))
            max_bindings = config['max_bindings_per_qq']
            
            if current_bindings >= max_bindings:
                return (
                    f"[CQ:at,qq={user_id}] 绑定失败\n"
                    f"你已绑定 {current_bindings} 个游戏 ID，"
                    f"最多只能绑定 {max_bindings} 个\n"
                    f"(输入 查询绑定 查看已绑定的 ID)"
                )
            
            # 清理该QQ号之前的未使用验证码
            self._cleanup_previous_verify_codes(qq_id, server_key)
            
            # 生成验证码
            verify_code = self._get_unique_verify_code(server_key, config)
            verify_key = self._verify_storage_key(server_key, verify_code)
            expire_time = time.time() + config['verify_timeout']
            
            self.pending_verify[verify_key] = {
                'verify_code': verify_code,
                'qq_id': qq_id,
                'server_key': server_key,
                'server_name': server_name,
                'target_server': target_server,
                'expire_time': expire_time,
                'created_at': datetime.now().isoformat(),
                'used': False  # 标记为未使用
            }
            
            self.logger.info(f"为 QQ {qq_id} 生成验证码: {verify_code} (服务器: {server_name})")
            
            return (
                f"[CQ:at,qq={user_id}] 有效期: {config['verify_timeout']} 秒\n"
                f"服务器: {server_name}\n"
                f"验证码: {verify_code}\n"
                f"请在游戏内直接发送验证码"
            )
        
        except Exception as e:
            self.logger.error(f"处理绑定命令失败: {e}")
            return f"[CQ:at,qq={user_id}] 处理失败，请稍后重试"
    
    def _cleanup_previous_verify_codes(self, qq_id: str, server_key: Optional[str] = None):
        """清理该QQ号之前的未使用验证码"""
        try:
            codes_to_remove = []
            for code, info in self.pending_verify.items():
                same_server = not server_key or info.get('server_key', 'default') == server_key
                if info['qq_id'] == qq_id and same_server and not info.get('used', False):
                    codes_to_remove.append(code)
            
            for code in codes_to_remove:
                del self.pending_verify[code]
                self.logger.info(f"清理QQ {qq_id} 的旧验证码: {self._verify_display_code(code)}")
                
        except Exception as e:
            self.logger.error(f"清理旧验证码失败: {e}")

    def _get_unique_verify_code(self, server_key: str, config: Optional[Dict[str, Any]] = None) -> str:
        """生成唯一的验证码"""
        while True:
            code = self._generate_verify_code(config)
            storage_key = self._verify_storage_key(server_key, code)
            # 检查验证码是否已存在且未过期
            if storage_key not in self.pending_verify:
                return code
            else:
                # 如果验证码已存在但已过期，可以重用
                verify_info = self.pending_verify[storage_key]
                if time.time() > verify_info['expire_time']:
                    del self.pending_verify[storage_key]
                    return code

    async def handle_query_command(self, user_id: int, group_id: int,
                               command_text: str, **kwargs) -> Optional[str]:
        """处理查询绑定命令"""
        try:
            qq_id = str(user_id)
            target_server = kwargs.get('target_server') or self._active_target_server()
            bindings = self._bindings_for_qq(qq_id, target_server)
            
            if not bindings:
                return (
                    f"[CQ:at,qq={user_id}] 你还没有绑定任何游戏 ID\n"
                    f"输入 绑定 开始绑定"
                )
            
            response = f"[CQ:at,qq={user_id}] 当前服务器有 {len(bindings)} 个绑定:\n\n"
            
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
            target_server = kwargs.get('target_server') or self._active_target_server()
            server_key = self._server_key(target_server)
            
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
                if self._binding_server_key(binding) == server_key and binding['game_id'] == game_id:
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
            target_server = kwargs.get('target_server') or self._active_target_server()
            server_key = self._server_key(target_server)
            server_name = self._server_name(target_server)
            
            if command == 'list':
                filtered_data = {
                    qq_id: [
                        binding for binding in bindings
                        if self._binding_server_key(binding) == server_key
                    ]
                    for qq_id, bindings in self.binding_data.items()
                }
                filtered_data = {qq_id: bindings for qq_id, bindings in filtered_data.items() if bindings}
                if not filtered_data:
                    return f"{server_name} 没有任何绑定信息"
                
                response = f"服务器: {server_name}\n"
                response += f"总绑定数: {sum(len(v) for v in filtered_data.values())}\n"
                response += f"涉及 QQ: {len(filtered_data)} 个\n\n"
                
                for qq_id, bindings in filtered_data.items():
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
                    if self._binding_server_key(binding) == server_key and binding['game_id'] == game_id:
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
