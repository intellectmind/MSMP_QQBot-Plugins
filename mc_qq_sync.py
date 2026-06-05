import json
import re
import time
import asyncio
import copy
from pathlib import Path
from typing import Dict, Optional, List, Any
from datetime import datetime
from plugin_manager import BotPlugin


class MCQQSyncPlugin(BotPlugin):
    """MC-QQ消息同步插件"""
    
    name = "MC-QQ消息同步"
    version = "2.0.0"
    author = "MSMP_QQBot"
    description = "实现MC玩家消息与QQ群内消息的双向同步"
    DEFAULT_CONFIG = {
        'features': {
            'mc_auto_sync_to_qq': {'enabled': False, 'group_ids': [123456789]},
            'mc_manual_sync_to_qq': {'enabled': True, 'group_ids': [123456789]},
            'qq_manual_to_mc': {'enabled': True, 'group_ids': [123456789]}
        },
        'message_format': {
            'mc_auto_to_qq': '[MC] {player}: {message}',
            'mc_manual_to_qq': '[MC] {player}: {message}',
            'qq_manual_to_mc': '[QQ] {nickname}: {message}'
        },
        'qq_commands': {'mc_command_prefix': 'mc'},
        'mc_commands': {'qq_command_prefix': 'qq'},
        'blacklist': {'players': [], 'users': []},
        'chat_message_pattern': r'.*(?:\[Not Secure\]\s*)?<([^>]+)>\s*(.+)'
    }
    FLEXIBLE_CHAT_MESSAGE_PATTERN = r'.*(?:\[Not Secure\]\s*)?<([^>]+)>\s*(.+)'
    
    def __init__(self, logger):
        super().__init__(logger)
        self.dependencies = ["qq_mc_binding.qq_mc_binding"]
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
        self._seeded_log_sources = set()
        
        # 聊天消息匹配模式
        self.chat_message_pattern = self.FLEXIBLE_CHAT_MESSAGE_PATTERN
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件，不存在则创建默认配置"""
        default_config = copy.deepcopy(self.DEFAULT_CONFIG)
        
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                
                merged_config = self._deep_merge(default_config, loaded_config)
                
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
                names=self._qq_command_names(),
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
                self.config = self._deep_merge(self.config, new_plugin_config)
                self._save_config()
                self._refresh_mc_command_aliases()
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
                
                # 检查每个运行中服务器的MC玩家消息，避免 active 切换导致跨服串日志。
                for target_server in self._running_target_servers():
                    active_config = self._config_for_server(target_server)
                    auto_enabled = active_config['features']['mc_auto_sync_to_qq']['enabled']
                    manual_enabled = active_config['features']['mc_manual_sync_to_qq']['enabled']

                    if auto_enabled or manual_enabled:
                        await self._check_server_logs(target_server)
                
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
                self._server_running = bool(self._running_target_servers())
            else:
                self._server_running = True
        except Exception as e:
            self.logger.error(f"检查服务器状态失败: {e}")
            self._server_running = True
    
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

    async def _check_server_logs(self, target_server: Optional[Dict[str, Any]] = None):
        """检查服务器日志中的玩家消息"""
        try:
            self._cleanup_expired_cache()
            
            if not self.plugin_manager:
                return
            
            if hasattr(self.plugin_manager, 'get_incremental_server_logs'):
                logs = self.plugin_manager.get_incremental_server_logs(
                    "mc_qq_sync",
                    1000,
                    target_server
                )
            else:
                logs = self.plugin_manager.get_server_logs(200, target_server)
            
            if not isinstance(logs, list) or not logs:
                return

            server_key = self._server_key(target_server)
            if self._should_seed_log_source(target_server, server_key):
                for log_line in logs:
                    if isinstance(log_line, str):
                        self._processed_log_timestamps.add(self._get_log_hash(f"{server_key}::{log_line}"))
                self._seeded_log_sources.add(server_key)
                return
            
            for log_line in reversed(logs):
                if isinstance(log_line, str):
                    log_hash = self._get_log_hash(f"{server_key}::{log_line}")
                    
                    if log_hash in self._processed_log_timestamps:
                        continue
                    
                    processed = await self._process_player_message(log_line, target_server)
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
            if len(self._processed_log_timestamps) > 5000:
                timestamps_list = list(self._processed_log_timestamps)
                remove_count = len(timestamps_list) // 2
                for i in range(remove_count):
                    self._processed_log_timestamps.discard(timestamps_list[i])
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
    
    async def _process_player_message(self, log_line: str, target_server: Optional[Dict[str, Any]] = None) -> bool:
        """处理玩家消息"""
        try:
            if '<' not in log_line or '>' not in log_line:
                return False

            active_config = self._config_for_server(target_server)
            configured_pattern = active_config.get('chat_message_pattern') or self.chat_message_pattern
            match = None
            for pattern in dict.fromkeys([configured_pattern, self.FLEXIBLE_CHAT_MESSAGE_PATTERN]):
                match = re.search(pattern, log_line)
                if match:
                    break
            
            if not match:
                return False
            
            player_name = match.group(1).strip()
            message = match.group(2).strip()
            
            self.logger.debug(f"捕获到玩家消息: {player_name} -> {message}")
            
            # 检查玩家是否在黑名单中
            if player_name in active_config['blacklist']['players']:
                self.logger.debug(f"玩家 {player_name} 在黑名单中，跳过处理")
                return False
            
            qq_prefix = str(active_config['mc_commands'].get('qq_command_prefix') or 'qq').strip()
            qq_command = f"{qq_prefix} "
            # 检查是否是 MC 内主动发送到 QQ 的命令。
            if message.startswith(qq_command):
                qq_message = message[len(qq_prefix):].strip()
                if not qq_message:
                    self.logger.debug(f"玩家 {player_name} 的{qq_prefix}命令消息为空，跳过处理")
                    return False
                
                # 检查主动发送到QQ功能是否启用
                if not active_config['features']['mc_manual_sync_to_qq']['enabled']:
                    self.logger.debug("MC主动发送到QQ功能已禁用")
                    return False
                
                # 检查群列表是否为空
                if not self._target_group_ids('mc_manual_sync_to_qq', target_server, active_config):
                    self.logger.debug("MC主动发送到QQ的群列表为空")
                    return False
                
                cache_key = f"{self._server_key(target_server)}:{player_name}:{qq_prefix}:{qq_message}"
                if cache_key in self.message_cache:
                    self.logger.debug(f"消息缓存中已存在: {cache_key}，跳过处理")
                    return False
                
                self.message_cache[cache_key] = time.time()
                self.logger.info(f"处理MC主动发送到QQ: {player_name} -> {qq_message}")
                await self._forward_player_message_to_qq(player_name, qq_message, 'mc_manual_to_qq', target_server)
                return True
            
            mc_prefix = str(active_config['qq_commands'].get('mc_command_prefix') or 'mc').strip()
            # 过滤其他命令
            if message.startswith('/') or message.startswith(f'{mc_prefix} '):
                self.logger.debug(f"玩家 {player_name} 发送了命令，跳过处理: {message}")
                return False
            
            # 检查自动同步是否可用
            if not active_config['features']['mc_auto_sync_to_qq']['enabled']:
                self.logger.debug("MC自动同步到QQ功能已禁用")
                return False
            
            # 检查群列表是否为空
            if not self._target_group_ids('mc_auto_sync_to_qq', target_server, active_config):
                self.logger.debug("MC自动同步到QQ的群列表为空")
                return False
            
            cache_key = f"{self._server_key(target_server)}:{player_name}:{message}"
            if cache_key in self.message_cache:
                self.logger.debug(f"消息缓存中已存在: {cache_key}，跳过处理")
                return False
            
            self.message_cache[cache_key] = time.time()
            self.logger.info(f"处理MC自动同步到QQ: {player_name} -> {message}")
            await self._forward_player_message_to_qq(player_name, message, 'mc_auto_to_qq', target_server)
            return True
            
        except Exception as e:
            self.logger.error(f"处理玩家消息失败: {e}", exc_info=True)
            return False
    
    async def _forward_player_message_to_qq(
        self,
        player_name: str,
        message: str,
        msg_type: str = 'mc_auto_to_qq',
        target_server: Optional[Dict[str, Any]] = None
    ):
        """将玩家消息转发到QQ群"""
        try:
            # 根据消息类型确定功能和群列表
            active_config = self._config_for_server(target_server)
            if msg_type == 'mc_auto_to_qq':
                feature_config = active_config['features']['mc_auto_sync_to_qq']
            elif msg_type == 'mc_manual_to_qq':
                feature_config = active_config['features']['mc_manual_sync_to_qq']
            else:
                return
            
            if not feature_config['enabled']:
                return
            
            group_ids = self._target_group_ids(
                'mc_auto_sync_to_qq' if msg_type == 'mc_auto_to_qq' else 'mc_manual_sync_to_qq',
                target_server,
                config=active_config
            )
            if not group_ids:
                return
            
            if not self.plugin_manager.qq_server:
                return
            
            formatted_message = active_config['message_format'][msg_type].format(
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
            target_server = kwargs.get('target_server') or self._active_target_server()
            active_config = self._config_for_server(target_server)
            
            # 检查QQ->MC功能是否启用
            if not active_config['features']['qq_manual_to_mc']['enabled']:
                msg = f"[CQ:at,qq={user_id}] QQ->MC消息功能已禁用"
                self.logger.debug(msg)
                return msg
            
            # 检查群是否在允许列表
            allowed_groups = self._target_group_ids('qq_manual_to_mc', target_server, active_config)
            if group_id not in allowed_groups:
                msg = f"[CQ:at,qq={user_id}] 本群未启用该功能"
                self.logger.debug(f"{msg} (群ID: {group_id}, 允许群: {allowed_groups})")
                return msg
            
            # 检查用户是否绑定游戏ID
            if self.qq_mc_binding_plugin:
                qq_id = str(user_id)
                game_id = self._get_game_id_by_qq(qq_id, target_server)
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
            if user_id in active_config['blacklist']['users']:
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
                game_id = self._get_game_id_by_qq(str(user_id), target_server)
            
            # 格式化消息
            if game_id:
                formatted_message = f"《{game_id}》{message}"
            else:
                formatted_message = f"《QQ {user_id}》{message}"
            
            # 发送到MC
            self.logger.debug(f"正在发送消息到MC: {formatted_message}")
            sent = await self._send_message_to_mc(
                formatted_message,
                rcon_client=kwargs.get('target_rcon_client') or kwargs.get('rcon_client'),
                target_server=target_server
            )
            if not sent:
                return f"[CQ:at,qq={user_id}] 发送失败：目标MC服务器未连接或不可写入"
            
            self.logger.info(f"QQ {user_id}({qq_nickname}) 发送消息到MC: {message}")
            return f"[CQ:at,qq={user_id}] 消息已发送到MC服务器"
            
        except Exception as e:
            self.logger.error(f"处理MC命令失败: {e}", exc_info=True)
            return f"[CQ:at,qq={user_id}] 处理失败，请稍后重试"
    
    def _get_game_id_by_qq(self, qq_id: str, target_server: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """通过QQ号获取绑定的游戏ID"""
        try:
            if not self.qq_mc_binding_plugin:
                return None
            
            if hasattr(self.qq_mc_binding_plugin, 'binding_data'):
                binding_data = self.qq_mc_binding_plugin.binding_data
                if qq_id in binding_data and binding_data[qq_id]:
                    server_key = self._server_key(target_server)
                    for binding in binding_data[qq_id]:
                        binding_key = self._binding_server_key(binding)
                        if binding_key == server_key:
                            return binding.get('game_id')
            
            return None
        except Exception as e:
            self.logger.error(f"获取游戏ID失败: {e}")
            return None
    
    def _active_target_server(self) -> Dict[str, Any]:
        if self.plugin_manager and getattr(self.plugin_manager, 'qq_server', None):
            return getattr(self.plugin_manager.qq_server, 'active_server_config', None) or {}
        return {}

    def _server_key(self, target_server: Optional[Dict[str, Any]] = None) -> str:
        server = target_server or self._active_target_server()
        return str((server or {}).get('_config_file') or (server or {}).get('name') or 'default')

    def _binding_server_key(self, binding: Dict[str, Any]) -> str:
        if self.qq_mc_binding_plugin and hasattr(self.qq_mc_binding_plugin, '_binding_server_key'):
            return self.qq_mc_binding_plugin._binding_server_key(binding)
        return str((binding or {}).get('server_key') or (binding or {}).get('server') or 'default')

    def _qq_command_names(self) -> List[str]:
        """收集全局和每服务器配置中的 QQ->MC 命令前缀。"""
        names = {str(self.config['qq_commands'].get('mc_command_prefix') or 'mc').strip() or 'mc', 'mc'}
        if self.plugin_manager and hasattr(self.plugin_manager, 'get_configured_servers'):
            for server in self.plugin_manager.get_configured_servers():
                try:
                    prefix = self._config_for_server(server)['qq_commands'].get('mc_command_prefix')
                    if prefix:
                        names.add(str(prefix).strip().lower())
                except Exception as e:
                    self.logger.debug(f"读取服务器 MC 同步命令前缀失败: {e}")
        return sorted(names)

    def _refresh_mc_command_aliases(self) -> None:
        if not self.plugin_manager:
            return
        command_info = getattr(self.plugin_manager, 'command_handlers', {}).get('mc_message')
        if not command_info:
            return
        names = self._qq_command_names()
        command_info['names'] = names
        command_info['normalized_names'] = {str(name).lower() for name in names}

    def _config_for_server(self, target_server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """读取目标服务器独立插件配置，找不到则回退插件根配置。"""
        config = self._deep_merge(self.DEFAULT_CONFIG, self.config or {})
        if self.plugin_manager and hasattr(self.plugin_manager, 'get_plugin_server_file'):
            path = self.plugin_manager.get_plugin_server_file(
                "mc_qq_sync", "config.json", target_server or self._active_target_server(), create_parent=False
            )
            try:
                if path.exists():
                    with open(path, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        config = self._deep_merge(config, loaded)
            except Exception as e:
                self.logger.error(f"读取服务器插件配置失败 {path}: {e}")
        return config

    def _save_config_for_server(self, config: Dict[str, Any],
                                target_server: Optional[Dict[str, Any]] = None) -> None:
        """保存目标服务器独立插件配置。"""
        if self.plugin_manager and hasattr(self.plugin_manager, 'get_plugin_server_file'):
            path = self.plugin_manager.get_plugin_server_file(
                "mc_qq_sync", "config.json", target_server or self._active_target_server(), create_parent=True
            )
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                self.logger.debug(f"服务器插件配置已保存: {path}")
                return
            except Exception as e:
                self.logger.error(f"保存服务器插件配置失败 {path}: {e}")

        self.config = self._deep_merge(self.DEFAULT_CONFIG, config or {})
        self._save_config()

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _target_group_ids(self, feature_key: str, target_server: Optional[Dict[str, Any]] = None,
                          config: Optional[Dict[str, Any]] = None) -> List[int]:
        """优先使用目标服务器 QQ 群，避免插件全局群配置跨服串消息。"""
        server_groups = ((target_server or self._active_target_server()).get('qq') or {}).get('groups') or []
        if server_groups:
            return list(server_groups)
        active_config = config or self._config_for_server(target_server)
        return list((active_config.get('features') or {}).get(feature_key, {}).get('group_ids') or [])

    async def _send_message_to_mc(self, message: str, rcon_client=None,
                                  target_server: Optional[Dict[str, Any]] = None) -> bool:
        """发送消息到MC服务器"""
        try:
            self.logger.debug(f"_send_message_to_mc 被调用，消息: {message}")
            
            if not self.plugin_manager:
                self.logger.error("plugin_manager 为 None")
                return False
            
            if not self.plugin_manager.qq_server:
                self.logger.error("qq_server 为 None")
                return False
            
            cmd = f"say {message}"
            rcon = rcon_client
            explicit_target = bool(target_server)
            if rcon:
                if hasattr(rcon, 'run_connected'):
                    connected, _ = await asyncio.to_thread(
                        rcon.run_connected,
                        lambda client: client.execute_command(cmd)
                    )
                    if not connected:
                        self.logger.warning("目标RCON连接未建立")
                        return False
                    return True

                if not await asyncio.to_thread(rcon.is_connected):
                    self.logger.warning("目标RCON连接未建立")
                    return False

                self.logger.debug(f"执行目标RCON命令: {cmd}")
                await asyncio.to_thread(rcon.execute_command, cmd)
                self.logger.info(f"已通过目标RCON发送消息到MC: {message}")
                return True

            executor = getattr(self.plugin_manager.qq_server, '_execute_server_command', None)
            if callable(executor):
                await executor(cmd, target_server or self._active_target_server())
                self.logger.info(f"已通过目标服务器执行器发送消息到MC: {message}")
                return True

            if explicit_target:
                self.logger.warning("目标服务器缺少按服执行器，拒绝回退到全局RCON")
                return False

            rcon = getattr(self.plugin_manager.qq_server, 'rcon_client', None)
            if not rcon:
                self.logger.error("rcon_client 为 None")
                return False

            if not await asyncio.to_thread(rcon.is_connected):
                self.logger.warning("RCON连接未建立")
                return False

            self.logger.debug(f"执行RCON命令: {cmd}")
            await asyncio.to_thread(rcon.execute_command, cmd)
            self.logger.info(f"已通过RCON发送消息到MC: {message}")
            return True
            
        except Exception as e:
            self.logger.error(f"发送消息到MC失败: {e}", exc_info=True)
            return False
    
    async def handle_sync_config_command(self, user_id: int, group_id: int,
                                        command_text: str, **kwargs) -> Optional[str]:
        """处理同步配置命令"""
        try:
            args = command_text.strip().split() if command_text else []
            target_server = kwargs.get('target_server') or self._active_target_server()
            active_config = self._config_for_server(target_server)
            
            if not args:
                return self._get_sync_config_info(active_config, target_server)
            
            command = args[0].lower()
            
            if command == 'show':
                return self._get_sync_config_info(active_config, target_server)
            
            elif command == 'enable':
                if len(args) < 2:
                    return "用法: sync_config enable <auto_mc|manual_mc|manual_qq>"
                
                mode = args[1].lower()
                if mode == 'auto_mc':
                    active_config['features']['mc_auto_sync_to_qq']['enabled'] = True
                    self._save_config_for_server(active_config, target_server)
                    return "已启用MC玩家自动同步到QQ"
                elif mode == 'manual_mc':
                    active_config['features']['mc_manual_sync_to_qq']['enabled'] = True
                    self._save_config_for_server(active_config, target_server)
                    return "已启用MC玩家主动发送消息到QQ"
                elif mode == 'manual_qq':
                    active_config['features']['qq_manual_to_mc']['enabled'] = True
                    self._save_config_for_server(active_config, target_server)
                    return "已启用QQ用户通过命令发送消息到MC"
                else:
                    return "无效的模式"
            
            elif command == 'disable':
                if len(args) < 2:
                    return "用法: sync_config disable <auto_mc|manual_mc|manual_qq>"
                
                mode = args[1].lower()
                if mode == 'auto_mc':
                    active_config['features']['mc_auto_sync_to_qq']['enabled'] = False
                    self._save_config_for_server(active_config, target_server)
                    return "已禁用MC玩家自动同步到QQ"
                elif mode == 'manual_mc':
                    active_config['features']['mc_manual_sync_to_qq']['enabled'] = False
                    self._save_config_for_server(active_config, target_server)
                    return "已禁用MC玩家主动发送消息到QQ"
                elif mode == 'manual_qq':
                    active_config['features']['qq_manual_to_mc']['enabled'] = False
                    self._save_config_for_server(active_config, target_server)
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
                
                group_ids = active_config['features'][feature_key].setdefault('group_ids', [])
                if group_id_to_add not in group_ids:
                    group_ids.append(group_id_to_add)
                    self._save_config_for_server(active_config, target_server)
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
                
                group_ids = active_config['features'][feature_key].setdefault('group_ids', [])
                if group_id_to_remove in group_ids:
                    group_ids.remove(group_id_to_remove)
                    self._save_config_for_server(active_config, target_server)
                    return f"已从{description}列表中移除群{group_id_to_remove}"
                else:
                    return f"群{group_id_to_remove}不在{description}列表中"
            
            else:
                return "未知的命令"
        
        except Exception as e:
            self.logger.error(f"处理同步配置命令失败: {e}")
            return "处理失败，请查看日志"
    
    def _get_sync_config_info(self, config: Optional[Dict[str, Any]] = None,
                              target_server: Optional[Dict[str, Any]] = None) -> str:
        """获取同步配置信息"""
        active_config = config or self.config
        server_name = (target_server or {}).get('name') or self._server_key(target_server)
        response = f"【消息同步配置】\n服务器: {server_name}\n\n"
        
        response += "MC玩家自动同步到QQ:\n"
        response += f"  状态: {'已启用' if active_config['features']['mc_auto_sync_to_qq']['enabled'] else '已禁用'}\n"
        response += f"  群列表: {', '.join(map(str, active_config['features']['mc_auto_sync_to_qq']['group_ids'])) if active_config['features']['mc_auto_sync_to_qq']['group_ids'] else '无'}\n"
        response += f"  说明: MC玩家的聊天消息自动同步到指定QQ群\n\n"
        
        response += "MC玩家主动发送消息到QQ:\n"
        response += f"  状态: {'已启用' if active_config['features']['mc_manual_sync_to_qq']['enabled'] else '已禁用'}\n"
        response += f"  群列表: {', '.join(map(str, active_config['features']['mc_manual_sync_to_qq']['group_ids'])) if active_config['features']['mc_manual_sync_to_qq']['group_ids'] else '无'}\n"
        mc_to_qq_prefix = active_config['mc_commands']['qq_command_prefix']
        qq_to_mc_prefix = active_config['qq_commands']['mc_command_prefix']
        response += f"  说明: 使用 {mc_to_qq_prefix} <消息> 命令发送消息到QQ群\n\n"
        
        response += "QQ用户通过命令发送消息到MC:\n"
        response += f"  状态: {'已启用' if active_config['features']['qq_manual_to_mc']['enabled'] else '已禁用'}\n"
        response += f"  群列表: {', '.join(map(str, active_config['features']['qq_manual_to_mc']['group_ids'])) if active_config['features']['qq_manual_to_mc']['group_ids'] else '无'}\n"
        response += f"  说明: 使用 {qq_to_mc_prefix} <消息> 命令发送消息到MC服务器\n\n"
        
        response += "命令前缀:\n"
        response += f"  MC->QQ: {mc_to_qq_prefix}\n"
        response += f"  QQ->MC: {qq_to_mc_prefix}\n\n"
        
        response += "黑名单:\n"
        response += f"  玩家: {', '.join(active_config['blacklist']['players']) if active_config['blacklist']['players'] else '无'}\n"
        response += f"  用户: {', '.join(map(str, active_config['blacklist']['users'])) if active_config['blacklist']['users'] else '无'}\n"
        
        return response
    
    def get_plugin_help(self) -> str:
        """获取插件帮助"""
        active_config = self._config_for_server()
        mc_to_qq_prefix = active_config['mc_commands']['qq_command_prefix']
        qq_to_mc_prefix = active_config['qq_commands']['mc_command_prefix']
        return f"""
【MC-QQ消息同步】v{self.version}
作者: {self.author}
说明: {self.description}

功能:
• MC玩家聊天消息自动同步到QQ群
• MC玩家聊天消息主动发送到QQ群
• QQ群消息主动发送到MC服务器

MC游戏内命令:
  • {mc_to_qq_prefix} <消息>
    功能: 主动发送消息到QQ群 (功能2)
    示例: {mc_to_qq_prefix} 大家好

QQ群内命令:
  • {qq_to_mc_prefix} <消息>
    功能: 通过命令发送消息到MC服务器 (功能3)
    说明: 需在QQ-MC绑定插件中绑定游戏ID
    示例: {qq_to_mc_prefix} 你好

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
