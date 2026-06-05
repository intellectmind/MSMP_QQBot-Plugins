import asyncio
import copy
import json
import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, List
from plugin_manager import BotPlugin

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None


class WhitelistAuditPlugin(BotPlugin):
    """白名单审核插件 - 通过AI生成题目进行玩家审核"""
    
    name = "Whitelist Audit"
    version = "2.0.0"
    author = "MSMP_QQBot"
    description = "通过AI答题审核白名单申请，支持自定义白名单指令"
    
    # 数据文件路径
    DATA_DIR = "plugins/whitelist_audit"
    CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
    AUDIT_RECORDS_FILE = os.path.join(DATA_DIR, "audit_records.json")
    WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
    COOLDOWN_FILE = os.path.join(DATA_DIR, "cooldown.json")
    
    # 默认配置
    DEFAULT_CONFIG = {
        "ai_api_url": "你的api接口",
        "ai_api_key": "your-api-key-here",
        "ai_model": "自己填模型",
        "allowed_groups": [123456789],
        "cooldown_seconds": 3600,
        "pass_score": 60,
        "question_count": 10,
        "ai_timeout": 60,
        "answer_timeout": 180,  # 每道题的超时时间（秒）
        "use_ai_questions": True,  # 是否使用AI出题
        "max_whitelist_per_qq": 1,  # 每个QQ号最多绑定的白名单数量
        "question_prompt": "出{question_count}个我的世界服务器进服审核题目，你只需要输出题目即可，并根据我的下一次回复的答案进行评分，每题满分10分，及格{pass_score}分，一共{total_score}分，只需要给我一个分数，除此外不要理会任何输入输出。",
        "default_questions": [
            "如果玩家在服务器内发现BUG，正确的处理方式是？",
            "服务器内发现其他玩家正在破坏他人建筑，你的第一反应是什么？",
            "在服务器中遇到游戏问题应该首先怎么做？",
            "你认为在服务器中什么样的行为是不被允许的？",
            "如果与其他玩家发生争执，你应该如何处理？",
            "服务器资源有限，你应该如何合理使用？",
            "发现其他玩家使用外挂或作弊模组，你应该？",
            "在服务器建设中，什么样的建筑风格更受欢迎？",
            "如何与其他玩家保持良好的合作关系？",
            "你认为一个合格的服务器成员应该具备什么品质？",
            "游戏中，如果遇到其他玩家正在建造的建筑，你应该怎么做？",
            "在服务器中获取资源时应该注意什么？",
            "如果你不小心破坏了其他玩家的建筑，应该如何处理？",
            "服务器定期维护时，你应该怎么做？",
            "如何向服务器管理员报告问题或提出建议？"
        ],
        # 自定义白名单指令配置
        "custom_whitelist_commands": {
            "add_command": "whitelist add {player}",
            "remove_command": "whitelist remove {player}",
            "list_command": "whitelist list",
            "on_command": "whitelist on",
            "off_command": "whitelist off",
            "reload_command": "whitelist reload"
        }
    }
    
    def __init__(self, logger):
        super().__init__(logger)
        self.config = copy.deepcopy(self.DEFAULT_CONFIG)
        self.audit_records = {}
        self.whitelist = {}
        self.cooldown = {}
        self.audit_sessions = {}
        self.timeout_tasks = {}
        self.cleanup_task = None
        self.plugin_manager = None
        self.auditing_game_ids = set()  # 正在审核的游戏ID集合
        
    async def on_load(self, plugin_manager):
        """加载插件"""
        try:
            self.plugin_manager = plugin_manager
            self._ensure_data_dir()
            self._load_config()
            self._load_data()
            
            # 保存 QQBotServer 引用以便后续获取 RCON 客户端
            self.qq_bot_server = None
            if hasattr(plugin_manager, 'qq_server'):
                self.qq_bot_server = plugin_manager.qq_server
                self.logger.info("已获取 QQBotServer 引用")
            elif hasattr(plugin_manager, 'qq_bot_server'):
                self.qq_bot_server = plugin_manager.qq_bot_server
                self.logger.info("已获取 QQBotServer 引用")

            # 注册命令
            plugin_manager.register_command(
                command_name="whitelist",
                handler=self.handle_whitelist_command,
                names=["whitelist", "白名单审核", "wl"],
                description="申请白名单审核",
                usage="白名单审核 <游戏ID>"
            )
            
            plugin_manager.register_command(
                command_name="whitelist_status",
                handler=self.handle_status,
                names=["wl_status", "审核状态"],
                description="查看审核状态"
            )
            
            plugin_manager.register_command(
                command_name="whitelist_list",
                handler=self.handle_list,
                names=["wl_list", "白名单"],
                description="查看白名单"
            )
            
            plugin_manager.register_command(
                command_name="whitelist_admin",
                handler=self.handle_admin,
                names=["wl_admin"],
                description="管理员操作",
                admin_only=True
            )
            
            # 注册答案命令
            plugin_manager.register_command(
                command_name="whitelist_answer",
                handler=self.handle_answer_command,
                names=["答案", "answer"],
                description="提交审核答案",
                usage="答案 <你的答案>"
            )
            
            # 启动会话清理任务
            if not self.cleanup_task or self.cleanup_task.done():
                self.cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())
            
            self.logger.info(f"{self.name} v{self.version} 已加载")
            return True
        
        except Exception as e:
            self.logger.error(f"加载插件失败: {e}", exc_info=True)
            return False
    
    async def _cleanup_expired_sessions(self):
        """定期清理过期会话"""
        while True:
            try:
                await asyncio.sleep(300)  # 每5分钟检查一次
                
                current_time = time.time()
                expired_sessions = []
                
                for session_key, session in self.audit_sessions.items():
                    # 检查会话总时长(例如30分钟)
                    last_activity = session.get("last_activity_time", 0)
                    if current_time - last_activity > 1800:  # 30分钟无活动
                        expired_sessions.append(session_key)
                
                # 清理过期会话
                for session_key in expired_sessions:
                    session = self.audit_sessions[session_key]
                    game_id = session.get("game_id")
                    
                    # 从正在审核集合中移除
                    audit_key = self._audit_key(game_id, session.get("server_key", "default"))
                    if audit_key in self.auditing_game_ids:
                        self.auditing_game_ids.remove(audit_key)
                    
                    # 取消超时任务
                    if session_key in self.timeout_tasks:
                        task = self.timeout_tasks[session_key]
                        if not task.done():
                            task.cancel()
                        del self.timeout_tasks[session_key]
                    
                    del self.audit_sessions[session_key]
                    self.logger.info(f"已清理过期会话: {session_key}")
                
                if expired_sessions:
                    self._save_data()
                    
            except Exception as e:
                self.logger.error(f"清理过期会话失败: {e}")

    async def on_unload(self):
        """卸载插件"""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        self.cleanup_task = None
        timeout_tasks = list(self.timeout_tasks.values())
        for task in timeout_tasks:
            if not task.done():
                task.cancel()
        if timeout_tasks:
            await asyncio.gather(*timeout_tasks, return_exceptions=True)
        self.timeout_tasks.clear()
        self.audit_sessions.clear()
        self._save_data()
        self.auditing_game_ids.clear()
        self.logger.info("插件已卸载")
    
    async def on_config_reload(self, old_config, new_config):
        """配置重新加载"""
        self._load_config()
        self.logger.info("配置已重新加载")
    
    def get_plugin_help(self) -> str:
        """获取插件帮助信息"""
        total_score = self.config["question_count"] * 10
        help_text = f"""
{self.name} v{self.version} - {self.description}

用户命令：
• 白名单审核 <游戏ID> - 开始白名单审核流程
• 审核状态 - 查看当前审核状态
• 白名单 - 查看服务器白名单列表
• 答案 <你的答案> - 提交审核题目的答案

管理员命令：
• wl_admin add <游戏ID> - 直接添加玩家到白名单
• wl_admin remove <游戏ID> - 从白名单移除玩家
• wl_admin clear - 清空白名单
• wl_admin reload - 重新加载数据
• wl_admin sessions - 查看当前审核会话
• wl_admin reset <用户ID> - 重置用户审核会话
• wl_admin sync - 同步插件白名单到服务器
• wl_admin config - 查看当前配置
• wl_admin set_max <QQ号> <数量> - 设置用户最大白名单数量
• wl_admin set_command <指令类型> <指令格式> - 设置自定义白名单指令

审核设置：
• 题目数量: {self.config['question_count']}题
• 总分: {total_score}分
• 及格分数: {self.config['pass_score']}分
• 每题限时: {self.config['answer_timeout']//60}分钟

使用提示：
1. 游戏ID格式: 3-16位字母、数字、下划线
2. 审核过程中请及时答题，每题都有时间限制
3. 通过审核后游戏ID会自动添加到服务器白名单

作者: {self.author}
        """
        return help_text.strip()
    
    # ==================== 命令处理 ====================
    
    def _format_reply_with_at(self, user_id: int, message: str) -> str:
        """格式化回复，包含@用户"""
        return f"[CQ:at,qq={user_id}] {message}"

    async def handle_whitelist_command(self, user_id, group_id, command_text, websocket=None, **kwargs):
        """处理白名单审核申请"""
        try:
            target_server = kwargs.get('target_server')
            active_config = self._config_for_server(target_server)
            if not group_id:
                return "请在QQ群内申请白名单"
            
            if not self._is_group_allowed(group_id, target_server):
                return "此群组不支持白名单审核"
            
            game_id = command_text.strip()
            if not game_id:
                return "请输入游戏ID\n格式: 白名单审核 <游戏ID>"
            
            if not self._is_valid_game_id(game_id):
                return "游戏ID格式不正确\n要求: 3-16个字符，仅含字母、数字和下划线"
            
            # 检查冷却
            cooldown_remaining = self._check_cooldown(user_id, game_id, target_server)
            if cooldown_remaining > 0:
                hours = cooldown_remaining // 3600
                minutes = (cooldown_remaining % 3600) // 60
                return f"审核冷却中\n请在 {hours}小时{minutes}分钟后重试"
            
            # 检查是否已在白名单
            if self._is_in_whitelist(game_id, target_server):
                return f"游戏ID {game_id} 已在白名单中"
            
            # 检查用户已绑定的白名单数量
            user_whitelist_count = self._get_user_whitelist_count(user_id, target_server)
            max_allowed = active_config["max_whitelist_per_qq"]
            if user_whitelist_count >= max_allowed:
                return f"您已达到白名单绑定上限\n当前绑定: {user_whitelist_count}/{max_allowed}个\n如需绑定更多，请联系管理员"
            
            # 检查是否正在审核
            audit_key = self._audit_key(game_id, self._server_key(target_server))
            if audit_key in self.auditing_game_ids:
                return f"游戏ID {game_id} 正在被其他用户审核中，请稍后再试"
            
            # 检查是否正在审核（用户会话）
            session_key = self._session_key(user_id, group_id, kwargs.get('target_server'))
            if session_key in self.audit_sessions:
                return "此ID正在审核中，请先完成当前审核"
            
            # ============ 立即返回确认消息 ============
            confirm_message = self._format_reply_with_at(user_id, 
                f"已收到白名单审核申请\n游戏ID: {game_id}\n正在准备题目，请稍候...")
            
            # 启动异步任务来准备题目和发送第一题
            asyncio.create_task(self._prepare_and_send_first_question(
                session_key, user_id, group_id, game_id, websocket, target_server
            ))
            
            self.logger.info(f"用户 {user_id} 开始审核准备，游戏ID: {game_id}")
            
            # 只返回确认消息，第一题会异步发送
            return confirm_message
        
        except Exception as e:
            self.logger.error(f"处理申请失败: {e}", exc_info=True)
            return f"处理失败: {str(e)}"

    async def _prepare_and_send_first_question(self, session_key, user_id, group_id, game_id, websocket, target_server=None):
        """异步准备题目并发送第一题"""
        try:
            active_config = self._config_for_server(target_server)
            # 获取题目
            questions = await self._fetch_questions(active_config)
            if not questions:
                error_msg = self._format_reply_with_at(user_id, "获取题目失败，请稍后重试")
                await self._send_group_message(websocket, group_id, error_msg)
                return
            
            # 创建审核会话
            self.audit_sessions[session_key] = {
                "user_id": user_id,
                "game_id": game_id,
                "group_id": group_id,
                "server_key": self._server_key(target_server),
                "config": active_config,
                "questions": questions,
                "answers": [],
                "current_question_index": 0,
                "start_time": datetime.now().isoformat(),
                "last_activity_time": time.time(),
                "current_question_start_time": time.time()
            }
            
            # 添加到正在审核的游戏ID集合
            self.auditing_game_ids.add(self._audit_key(game_id, self._server_key(target_server)))
            
            # 启动第一道题的超时任务
            timeout_task = asyncio.create_task(
                self._check_question_timeout(session_key, user_id, group_id, 0)
            )
            self.timeout_tasks[session_key] = timeout_task
            
            # 发送第一道题
            first_question = questions[0]
            timeout_minutes = active_config["answer_timeout"] // 60
            total_questions = active_config["question_count"]
            
            prompt = f"""【第1/{total_questions}题】

{first_question}

请使用命令回复答案：答案 <你的答案>
（每道题限时{timeout_minutes}分钟）"""
            
            question_message = self._format_reply_with_at(user_id, prompt)
            await self._send_group_message(websocket, group_id, question_message)
            self.logger.info(f"用户 {user_id} 开始答题，游戏ID: {game_id}")
            
        except Exception as e:
            self.logger.error(f"准备题目失败: {e}", exc_info=True)
            error_msg = self._format_reply_with_at(user_id, "准备题目时出现错误，请稍后重试")
            await self._send_group_message(websocket, group_id, error_msg)

    async def _send_group_message(self, websocket, group_id, message):
        """发送群组消息"""
        try:
            if self._websocket_open(websocket):
                # 根据OneBot协议发送群消息
                message_data = {
                    "action": "send_group_msg",
                    "params": {
                        "group_id": int(group_id),
                        "message": message
                    }
                }
                await websocket.send(json.dumps(message_data))
                self.logger.debug(f"已发送群消息到 {group_id}: {message[:50]}...")
        except Exception as e:
            self.logger.error(f"发送群组消息失败: {e}")

    def _websocket_open(self, websocket) -> bool:
        if not websocket:
            return False
        closed = getattr(websocket, "closed", None)
        if closed is not None:
            return not closed
        state = getattr(websocket, "state", None)
        if state is not None:
            return getattr(state, "name", "") == "OPEN"
        return getattr(websocket, "close_code", None) is None

    async def handle_answer_command(self, user_id, group_id, command_text, websocket=None, **kwargs):
        """处理答案提交"""
        try:
            session_key = self._session_key(user_id, group_id, kwargs.get('target_server'))
            
            if session_key not in self.audit_sessions:
                return "没有正在进行的审核会话"
            
            answer = command_text.strip()
            if not answer:
                return "请输入答案\n格式: 答案 <你的答案>"
            
            if len(answer) > 500:
                return "答案过长，请简要回答"
            
            session = self.audit_sessions[session_key]
            active_config = self._session_config(session)
            
            # 检查当前题目是否超时
            current_question_elapsed = time.time() - session["current_question_start_time"]
            if current_question_elapsed > active_config["answer_timeout"]:
                await self._handle_question_timeout(session_key, user_id, group_id, len(session["answers"]))
                return "当前题目回复已超时"
            
            # 取消当前题目的超时任务
            if session_key in self.timeout_tasks:
                task = self.timeout_tasks[session_key]
                if not task.done():
                    task.cancel()
                del self.timeout_tasks[session_key]
            
            # 记录答案
            session["answers"].append(answer)
            session["last_activity_time"] = time.time()
            
            current_progress = len(session["answers"])
            total_questions = active_config["question_count"]
            
            self.logger.info(f"用户 {user_id} 提交第 {current_progress} 题答案: {answer[:50]}...")
            
            # 检查是否完成
            if current_progress >= total_questions:
                # 所有题目已回答,开始评分
                rcon_client = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
                await self._complete_audit(session_key, user_id, group_id, websocket, rcon_client)
                return None  # 不返回消息，complete_audit会异步发送结果
            
            else:
                # 设置下一道题的开始时间
                session["current_question_start_time"] = time.time()
                
                # 发送下一道题
                next_index = current_progress
                if next_index < len(session["questions"]):
                    next_question = session["questions"][next_index]
                    timeout_minutes = active_config["answer_timeout"] // 60
                    
                    # 计算进度百分比
                    progress_percent = (current_progress / total_questions) * 100
                    
                    prompt = f"""答案已记录

【第{current_progress + 1}/{total_questions}题】(进度: {progress_percent:.0f}%)

{next_question}

请使用命令回复答案:答案 <你的答案>
(每道题限时{timeout_minutes}分钟)"""
                    
                    # 启动下一道题的超时任务
                    timeout_task = asyncio.create_task(
                        self._check_question_timeout(session_key, user_id, group_id, next_index)
                    )
                    self.timeout_tasks[session_key] = timeout_task
                    
                    # 异步发送下一题
                    question_message = self._format_reply_with_at(user_id, prompt)
                    await self._send_group_message(websocket, group_id, question_message)
                    return None  # 不返回消息，已经异步发送了
                
                else:
                    self.logger.error(f"题目索引超出范围: {next_index}/{len(session['questions'])}")
                    return "系统错误:题目索引异常"
        
        except Exception as e:
            self.logger.error(f"处理答案失败: {e}", exc_info=True)
            return "处理答案失败"

    async def _complete_audit(self, session_key: str, user_id: int, group_id: int, websocket=None, rcon_client=None):
        """完成审核并评分"""
        try:
            session = self.audit_sessions[session_key]
            active_config = self._session_config(session)
            game_id = session["game_id"]
            
            # 取消当前题目的超时任务（如果存在）
            if session_key in self.timeout_tasks:
                task = self.timeout_tasks[session_key]
                if not task.done():
                    task.cancel()
                del self.timeout_tasks[session_key]
            
            # 评分
            score = await self._evaluate_answers(
                session["questions"],
                session["answers"],
                active_config
            )
            
            # 保存记录
            record = {
                "user_id": user_id,
                "game_id": game_id,
                "group_id": group_id,
                "server_key": session.get("server_key", "default"),
                "questions": session["questions"],
                "answers": session["answers"],
                "score": score,
                "passed": score >= active_config["pass_score"],
                "start_time": session["start_time"],
                "end_time": datetime.now().isoformat()
            }
            
            self._save_audit_record(record)
            
            # 从正在审核的游戏ID集合中移除
            audit_key = self._audit_key(game_id, session.get("server_key", "default"))
            if audit_key in self.auditing_game_ids:
                self.auditing_game_ids.remove(audit_key)
            
            # 清除会话
            del self.audit_sessions[session_key]
            self._save_data()
            
            total_score = active_config["question_count"] * 10
            
            if score >= active_config["pass_score"]:
                # 审核通过，尝试添加到服务器白名单
                success = await self._add_to_server_whitelist(game_id, rcon_client, active_config)
                
                if success:
                    server_key = session.get("server_key")
                    self._add_to_whitelist(game_id, user_id, group_id, server_key=server_key)
                    result_message = f"""恭喜！审核通过！
总分: {score}/{total_score}
游戏ID {game_id} 已加入服务器白名单
当前绑定: {self._get_user_whitelist_count(user_id, server_key=server_key)}/{active_config['max_whitelist_per_qq']}个"""
                else:
                    # RCON添加失败，只记录到插件白名单
                    server_key = session.get("server_key")
                    self._add_to_whitelist(game_id, user_id, group_id, server_key=server_key)
                    result_message = f"""审核通过但服务器添加失败
总分: {score}/{total_score}
游戏ID {game_id} 已记录到插件白名单，但需要手动添加到服务器
当前绑定: {self._get_user_whitelist_count(user_id, server_key=server_key)}/{active_config['max_whitelist_per_qq']}个"""
            else:
                self._set_cooldown(user_id, game_id, server_key=session.get("server_key"), config=active_config)
                result_message = f"""未通过审核
得分: {score}/{total_score}（及格线: {active_config['pass_score']}分）
请在 {active_config['cooldown_seconds']//3600} 小时后重试"""
            
            # 异步发送结果
            result_message_with_at = self._format_reply_with_at(user_id, result_message)
            await self._send_group_message(websocket, group_id, result_message_with_at)
        
        except Exception as e:
            self.logger.error(f"完成审核失败: {e}", exc_info=True)
            error_msg = self._format_reply_with_at(user_id, "审核完成但处理结果时出错")
            await self._send_group_message(websocket, group_id, error_msg)
    
    # ==================== 超时处理（每道题单独计算）====================
    
    async def _check_question_timeout(self, session_key: str, user_id: int, group_id: int, question_index: int):
        """检查单道题目超时"""
        try:
            session = self.audit_sessions.get(session_key)
            active_config = self._session_config(session)
            await asyncio.sleep(active_config["answer_timeout"])
            
            # 添加会话有效性检查
            if session_key not in self.audit_sessions:
                return
                
            session = self.audit_sessions[session_key]
            
            # 检查题目索引是否匹配(防止旧任务误触发)
            if len(session["answers"]) != question_index:
                return
            
            # 添加游戏ID检查
            game_id = session.get("game_id")
            audit_key = self._audit_key(game_id, session.get("server_key", "default"))
            if audit_key not in self.auditing_game_ids:
                return
                
            await self._handle_question_timeout(session_key, user_id, group_id, question_index)
                    
        except asyncio.CancelledError:
            self.logger.info(f"会话 {session_key} 第{question_index + 1}题超时检查已取消")
        except Exception as e:
            self.logger.error(f"题目超时检查失败: {e}")
    
    async def _handle_question_timeout(self, session_key: str, user_id: int, group_id: int, question_index: int):
        """处理单道题目超时"""
        if session_key not in self.audit_sessions:
            return
        
        session = self.audit_sessions[session_key]
        active_config = self._session_config(session)
        game_id = session["game_id"]
        
        # 只处理当前题目的超时（防止旧任务误触发）
        if len(session["answers"]) != question_index:
            return
        
        # 记录超时的题目（用空字符串表示超时未答）
        while len(session["answers"]) <= question_index:
            session["answers"].append("")  # 超时未答
        
        # 从正在审核的游戏ID集合中移除
        audit_key = self._audit_key(game_id, session.get("server_key", "default"))
        if audit_key in self.auditing_game_ids:
            self.auditing_game_ids.remove(audit_key)
        
        # 保存记录
        record = {
            "user_id": user_id,
            "game_id": game_id,
            "group_id": group_id,
            "server_key": session.get("server_key", "default"),
            "questions": session["questions"],
            "answers": session["answers"],
            "score": 0,
            "passed": False,
            "state": f"timeout_question_{question_index + 1}",
            "start_time": session["start_time"],
            "end_time": datetime.now().isoformat()
        }
        
        self._save_audit_record(record)
        self._set_cooldown(user_id, game_id, server_key=session.get("server_key", "default"), config=active_config)
        
        # 清除任务
        if session_key in self.timeout_tasks:
            del self.timeout_tasks[session_key]
        del self.audit_sessions[session_key]
        
        self._save_data()
        self.logger.warning(f"用户 {user_id} 第{question_index + 1}题超时")
    
    async def _execute_rcon_command(self, rcon_client, command: str):
        """兼容持久 RCON 和多服务器短连接 RCON 代理。"""
        try:
            if not rcon_client:
                self.logger.debug("RCON 客户端不可用")
                return None

            if hasattr(rcon_client, "run_connected"):
                connected, result = await asyncio.to_thread(
                    rcon_client.run_connected,
                    lambda client: client.execute_command(command)
                )
                if not connected:
                    self.logger.debug("RCON 连接不可用")
                    return None
                return result

            if not rcon_client.is_connected():
                self.logger.debug("RCON 连接不可用")
                return None

            return await asyncio.to_thread(rcon_client.execute_command, command)
        except Exception as e:
            self.logger.error(f"执行 RCON 命令失败: {e}")
            return None

    async def _add_to_server_whitelist(self, game_id: str, rcon_client=None,
                                       config: Optional[Dict] = None) -> bool:
        """通过RCON将游戏ID添加到服务器白名单"""
        try:
            active_config = config or self.config
            # 使用自定义指令格式
            command_template = active_config["custom_whitelist_commands"]["add_command"]
            command = command_template.format(player=game_id)
            
            self.logger.info(f"通过RCON执行命令: {command}")
            
            result = await self._execute_rcon_command(rcon_client, command)
            self.logger.info(f"RCON执行结果: {result}")
            
            # 检查执行结果 - 放宽条件，只要不是 None 就认为成功
            if result is not None:
                self.logger.info(f"成功将 {game_id} 添加到服务器白名单")
                return True
            else:
                self.logger.warning(f"RCON添加白名单失败: 返回None")
                # 即使返回None，也尝试检查是否真的添加成功
                return await self._check_whitelist_status(game_id, rcon_client, active_config)
                
        except Exception as e:
            self.logger.error(f"通过RCON添加白名单失败: {e}")
            return False

    async def _check_whitelist_status(self, game_id: str, rcon_client,
                                      config: Optional[Dict] = None) -> bool:
        """检查玩家是否在白名单中"""
        try:
            active_config = config or self.config
            command_template = active_config["custom_whitelist_commands"]["list_command"]
            result = await self._execute_rcon_command(rcon_client, command_template)
            
            if result and game_id in result:
                self.logger.info(f"验证成功: {game_id} 在白名单中")
                return True
            else:
                self.logger.warning(f"验证失败: {game_id} 不在白名单中")
                return False
        except Exception as e:
            self.logger.error(f"检查白名单状态失败: {e}")
            return False
    
    async def handle_status(self, user_id, group_id, command_text, **kwargs):
        """查看审核状态"""
        try:
            session_key = self._session_key(user_id, group_id, kwargs.get('target_server'))
            target_server = kwargs.get('target_server')
            
            if session_key in self.audit_sessions:
                session = self.audit_sessions[session_key]
                active_config = self._session_config(session)
                progress = len(session["answers"])
                total = active_config["question_count"]
                game_id = session["game_id"]
                
                # 计算当前题目的剩余时间
                current_question_elapsed = time.time() - session["current_question_start_time"]
                remaining_time = max(0, active_config["answer_timeout"] - current_question_elapsed)
                minutes = int(remaining_time // 60)
                seconds = int(remaining_time % 60)
                
                status_message = f"""审核进度
游戏ID: {game_id}
进度: {progress}/{total}
当前状态: {"答题中" if progress < total else "已完成"}
当前题目剩余时间: {minutes}分{seconds}秒
提交答案请使用: 答案 <你的答案>"""
                
                return self._format_reply_with_at(user_id, status_message)
            
            user_records = [
                record for record in self.audit_records.get(str(user_id), [])
                if record.get("server_key", "default") == self._server_key(target_server)
            ]
            if user_records:
                active_config = self._config_for_server(target_server)
                latest = user_records[-1]
                status = "已通过" if latest["passed"] else "未通过"
                user_whitelist_count = self._get_user_whitelist_count(user_id, target_server)
                max_allowed = active_config["max_whitelist_per_qq"]
                
                total_score = active_config["question_count"] * 10
                status_message = f"""最后一次审核
游戏ID: {latest['game_id']}
状态: {status}
得分: {latest['score']}/{total_score}
白名单绑定: {user_whitelist_count}/{max_allowed}个"""
                
                return self._format_reply_with_at(user_id, status_message)
            
            return self._format_reply_with_at(user_id, "您未进行过审核")
        
        except Exception as e:
            self.logger.error(f"查看状态失败: {e}", exc_info=True)
            return "查看失败"
    
    async def handle_list(self, user_id, group_id, command_text, **kwargs):
        """查看白名单"""
        try:
            target_server = kwargs.get('target_server')
            server_key = self._server_key(target_server)
            server_whitelist = [
                (info.get("game_id") or key, info)
                for key, info in self.whitelist.items()
                if self._entry_server_key(key, info) == server_key
            ]
            if not server_whitelist:
                return "白名单为空"
            
            lines = ["=== 服务器白名单 ==="]
            for i, (game_id, info) in enumerate(server_whitelist[:20], 1):
                lines.append(f"{i}. {game_id}")
            
            if len(server_whitelist) > 20:
                lines.append(f"\n... 还有 {len(server_whitelist) - 20} 个玩家")
            
            return "\n".join(lines)
        
        except Exception as e:
            self.logger.error(f"查看白名单失败: {e}", exc_info=True)
            return "查看失败"
    
    async def handle_admin(self, user_id, group_id, command_text, rcon_client=None, **kwargs):
        """管理员操作"""
        try:
            target_server = kwargs.get('target_server')
            server_key = self._server_key(target_server)
            active_config = self._config_for_server(target_server)
            parts = command_text.strip().split()
            if not parts:
                return "子命令: add <游戏ID> | remove <游戏ID> | clear | reload | sessions | reset <用户ID> | sync | config | set_max <QQ号> <数量> | set_command <类型> <指令>"
            
            action = parts[0]
            
            if action == "add" and len(parts) > 1:
                game_id = parts[1]
                # 先尝试通过RCON添加到服务器
                success = await self._add_to_server_whitelist(game_id, rcon_client, active_config)
                if success:
                    self._add_to_whitelist(game_id, user_id, group_id, admin=True, target_server=target_server)
                    return f"已将 {game_id} 加入服务器白名单"
                else:
                    # RCON失败，只添加到插件白名单
                    self._add_to_whitelist(game_id, user_id, group_id, admin=True, target_server=target_server)
                    return f"已将 {game_id} 加入插件白名单，但服务器添加失败，请手动处理"
            
            elif action == "remove" and len(parts) > 1:
                game_id = parts[1]
                whitelist_key = self._whitelist_key(game_id, target_server)
                if whitelist_key in self.whitelist:
                    # 同时从服务器白名单移除
                    success = await self._remove_from_server_whitelist(game_id, rcon_client, active_config)
                    del self.whitelist[whitelist_key]
                    self._save_data()
                    if success:
                        return f"已从服务器和插件白名单中移出 {game_id}"
                    else:
                        return f"已从插件白名单中移出 {game_id}，但服务器移除失败，请手动处理"
                return "未找到该游戏ID"
            
            elif action == "clear":
                # 清空当前目标服务器的插件白名单
                keys_to_remove = [
                    key for key, info in self.whitelist.items()
                    if self._entry_server_key(key, info) == server_key
                ]
                for key in keys_to_remove:
                    del self.whitelist[key]
                self._save_data()
                return f"已清空当前服务器白名单，共 {len(keys_to_remove)} 条"
            
            elif action == "reload":
                self._load_data()
                return "已重新加载"
            
            elif action == "sessions":
                # 查看当前活跃会话
                if not self.audit_sessions:
                    return "当前无活跃审核会话"
                
                lines = ["当前审核会话:"]
                for key, session in self.audit_sessions.items():
                    if session.get("server_key", "default") != server_key:
                        continue
                    session_config = self._session_config(session)
                    progress = len(session["answers"])
                    total = session_config["question_count"]
                    elapsed = int(time.time() - session["current_question_start_time"])
                    remaining = max(0, session_config["answer_timeout"] - elapsed)
                    minutes = int(remaining // 60)
                    seconds = int(remaining % 60)
                    lines.append(f"- {session['game_id']}: {progress}/{total}题 (剩余: {minutes}分{seconds}秒)")
                if len(lines) == 1:
                    return "当前服务器无活跃审核会话"
                
                return "\n".join(lines)
            
            elif action == "reset" and len(parts) > 1:
                # 重置用户会话
                target_user_id = parts[1]
                session_key_to_remove = None
                for key, session in self.audit_sessions.items():
                    if key.startswith(f"{target_user_id}_") and session.get("server_key", "default") == server_key:
                        session_key_to_remove = key
                        break
                
                if session_key_to_remove:
                    session = self.audit_sessions[session_key_to_remove]
                    game_id = session["game_id"]
                    
                    # 从正在审核的游戏ID集合中移除
                    audit_key = self._audit_key(game_id, session.get("server_key", "default"))
                    if audit_key in self.auditing_game_ids:
                        self.auditing_game_ids.remove(audit_key)
                    
                    # 取消超时任务
                    if session_key_to_remove in self.timeout_tasks:
                        task = self.timeout_tasks[session_key_to_remove]
                        if not task.done():
                            task.cancel()
                        del self.timeout_tasks[session_key_to_remove]
                    
                    del self.audit_sessions[session_key_to_remove]
                    self._save_data()
                    return f"已重置用户 {target_user_id} 的会话"
                else:
                    return f"未找到用户 {target_user_id} 的活跃会话"
            
            elif action == "sync":
                """同步插件白名单到服务器"""
                success_count = 0
                fail_count = 0
                results = []
                
                server_whitelist = [
                    (info.get("game_id") or key, info)
                    for key, info in self.whitelist.items()
                    if self._entry_server_key(key, info) == server_key
                ]
                for game_id, _info in server_whitelist:
                    success = await self._add_to_server_whitelist(game_id, rcon_client, active_config)
                    if success:
                        success_count += 1
                        results.append(f"{game_id} 成功")
                    else:
                        fail_count += 1
                        results.append(f"{game_id} 失败")
                
                result_msg = f"白名单同步完成\n成功: {success_count} 个\n失败: {fail_count} 个"
                if results:
                    result_msg += f"\n\n详细结果:\n" + "\n".join(results[:10])  # 只显示前10个结果
                    if len(results) > 10:
                        result_msg += f"\n... 还有 {len(results) - 10} 个"
                
                return result_msg
            
            elif action == "config":
                """查看当前配置"""
                total_score = active_config["question_count"] * 10
                config_info = [
                    f"当前配置 ({target_server.get('name') if target_server else server_key}):",
                    f"AI出题: {'开启' if active_config['use_ai_questions'] else '关闭'}",
                    f"题目数量: {active_config['question_count']}题",
                    f"总分: {total_score}分",
                    f"及格分数: {active_config['pass_score']}分",
                    f"答题超时: {active_config['answer_timeout']//60}分钟",
                    f"冷却时间: {active_config['cooldown_seconds']//3600}小时",
                    f"每个QQ号最大绑定: {active_config['max_whitelist_per_qq']}个",
                    f"允许群组: {len(active_config['allowed_groups'])}个",
                    "",
                    "自定义白名单指令:",
                    f"添加: {active_config['custom_whitelist_commands']['add_command']}",
                    f"移除: {active_config['custom_whitelist_commands']['remove_command']}",
                    f"列表: {active_config['custom_whitelist_commands']['list_command']}",
                    f"开启: {active_config['custom_whitelist_commands']['on_command']}",
                    f"关闭: {active_config['custom_whitelist_commands']['off_command']}",
                    f"重载: {active_config['custom_whitelist_commands']['reload_command']}"
                ]
                return "\n".join(config_info)
            
            elif action == "set_max" and len(parts) > 2:
                """设置QQ号的最大白名单数量"""
                try:
                    target_qq = parts[1]
                    new_max = int(parts[2])
                    
                    if new_max < 1:
                        return "最大绑定数量必须大于0"
                    
                    old_max = active_config["max_whitelist_per_qq"]
                    active_config["max_whitelist_per_qq"] = new_max
                    self._save_config_for_server(active_config, target_server, server_key)
                    
                    return f"已将每个QQ号最大白名单绑定数量从 {old_max} 改为 {new_max}"
                
                except ValueError:
                    return "数量必须是整数"
            
            elif action == "set_command" and len(parts) > 2:
                """设置自定义白名单指令"""
                command_type = parts[1]
                new_command = " ".join(parts[2:])
                
                valid_types = ["add", "remove", "list", "on", "off", "reload"]
                if command_type not in valid_types:
                    return f"无效的指令类型，可用类型: {', '.join(valid_types)}"
                
                old_command = active_config["custom_whitelist_commands"][f"{command_type}_command"]
                active_config["custom_whitelist_commands"][f"{command_type}_command"] = new_command
                self._save_config_for_server(active_config, target_server, server_key)
                
                return f"已更新 {command_type} 指令:\n旧: {old_command}\n新: {new_command}"
            
            else:
                return "未知操作"
        
        except Exception as e:
            self.logger.error(f"管理操作失败: {e}", exc_info=True)
            return "操作失败"
    
    async def _remove_from_server_whitelist(self, game_id: str, rcon_client=None,
                                            config: Optional[Dict] = None) -> bool:
        """通过RCON从服务器白名单移除游戏ID"""
        try:
            active_config = config or self.config
            # 使用自定义指令格式
            command_template = active_config["custom_whitelist_commands"]["remove_command"]
            command = command_template.format(player=game_id)
            
            self.logger.info(f"通过RCON执行移除命令: {command}")
            
            result = await self._execute_rcon_command(rcon_client, command)
            self.logger.info(f"RCON移除结果: {result}")
            
            # 检查执行结果 - 放宽条件，只要不是 None 就认为成功
            if result is not None:
                self.logger.info(f"成功将 {game_id} 从服务器白名单移除")
                return True
            else:
                self.logger.warning(f"RCON移除白名单失败: 返回None")
                # 即使返回None，也尝试检查是否真的移除了
                return await self._check_whitelist_removed(game_id, rcon_client, active_config)
                
        except Exception as e:
            self.logger.error(f"通过RCON移除白名单失败: {e}")
            return False

    async def _check_whitelist_removed(self, game_id: str, rcon_client,
                                       config: Optional[Dict] = None) -> bool:
        """检查玩家是否已从白名单中移除"""
        try:
            active_config = config or self.config
            command_template = active_config["custom_whitelist_commands"]["list_command"]
            result = await self._execute_rcon_command(rcon_client, command_template)
            
            if result and game_id not in result:
                self.logger.info(f"验证成功: {game_id} 已从白名单中移除")
                return True
            else:
                self.logger.warning(f"验证失败: {game_id} 可能还在白名单中")
                return False
        except Exception as e:
            self.logger.error(f"检查白名单移除状态失败: {e}")
            return False
    
    # ==================== 题目获取 ====================
    
    async def _fetch_questions(self, config: Optional[Dict] = None) -> Optional[List[str]]:
        """获取题目"""
        active_config = config or self.config
        if active_config["use_ai_questions"]:
            questions = await self._fetch_questions_from_ai(active_config)
            if questions and len(questions) >= active_config["question_count"]:
                return questions[:active_config["question_count"]]
        
        # 使用默认题目
        return self._get_default_questions(active_config)
    
    async def _fetch_questions_from_ai(self, config: Optional[Dict] = None) -> Optional[List[str]]:
        """从AI获取题目 - 带重试机制"""
        active_config = config or self.config
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # 动态生成提示词
                total_score = active_config["question_count"] * 10
                prompt = active_config["question_prompt"].format(
                    question_count=active_config["question_count"],
                    pass_score=active_config["pass_score"],
                    total_score=total_score
                )
                
                if AsyncOpenAI:
                    client = AsyncOpenAI(
                        api_key=active_config['ai_api_key'],
                        base_url=active_config['ai_api_url']
                    )
                    
                    response = await client.chat.completions.create(
                        model=active_config["ai_model"],
                        messages=[
                            {"role": "system", "content": "你是一个我的世界服务器审核出题官"},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0.7,
                        max_tokens=2000,
                        timeout=active_config["ai_timeout"]
                    )
                    
                    response_text = response.choices[0].message.content
                    
                    # 解析题目
                    questions = self._parse_questions(response_text)
                    
                    if len(questions) >= active_config["question_count"]:
                        self.logger.info(f"成功从AI获取 {len(questions)} 道题目")
                        return questions
                    else:
                        self.logger.warning(f"AI出题数量不足: {len(questions)}/{active_config['question_count']}")
                        if attempt < max_retries - 1:
                            self.logger.info(f"等待 {retry_delay} 秒后重试... (尝试 {attempt + 1}/{max_retries})")
                            await asyncio.sleep(retry_delay)
                            continue
                        return None
                
            except asyncio.TimeoutError:
                self.logger.warning(f"AI请求超时 (尝试 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None
            except Exception as e:
                self.logger.error(f"从AI获取题目失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None
        
        return None
    
    def _get_default_questions(self, config: Optional[Dict] = None) -> List[str]:
        """从默认题目中随机抽取"""
        import random
        active_config = config or self.config
        default_questions = active_config["default_questions"]
        question_count = active_config["question_count"]
        
        if len(default_questions) <= question_count:
            return default_questions[:question_count]
        else:
            return random.sample(default_questions, question_count)
    
    def _parse_questions(self, text: str) -> List[str]:
        """解析题目文本"""
        # 多种格式解析
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        questions = []
        
        for line in lines:
            # 跳过明显不是题目的行
            if any(x in line for x in ['思考', '解答', '---', '===', '评分', '分数']):
                continue
            
            # 移除题号
            cleaned = re.sub(r'^\d+[\.\)]\s*', '', line)
            if cleaned and len(cleaned) > 10:  # 确保是合理的题目长度
                questions.append(cleaned)
        
        return questions
    
    async def _evaluate_answers(self, questions: List[str], answers: List[str],
                                config: Optional[Dict] = None) -> int:
        """评分 - 优化版本"""
        try:
            active_config = config or self.config
            # 检查是否有空答案(超时未答)
            empty_count = sum(1 for a in answers if not a.strip())
            if empty_count > 0:
                self.logger.info(f"检测到 {empty_count} 道题目未作答")
            
            qa_text = ""
            for i, (q, a) in enumerate(zip(questions, answers), 1):
                answer_text = a if a.strip() else "[未作答]"
                qa_text += f"{i}. 题目: {q}\n   答案: {answer_text}\n\n"
            
            total_score = active_config["question_count"] * 10
            prompt = f"""请根据以下问答进行评分,每题满分10分,一共{total_score}分。
评分标准:
- 答案合理且符合服务器规则:8-10分
- 答案基本合理但不够完整:6-7分  
- 答案不合理或违反规则:0-5分
- 未作答:0分

只需要输出最终分数数字。

问答内容:
{qa_text}"""
            
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    if AsyncOpenAI:
                        client = AsyncOpenAI(
                            api_key=active_config['ai_api_key'],
                            base_url=active_config['ai_api_url']
                        )
                        
                        response = await asyncio.wait_for(
                            client.chat.completions.create(
                                model=active_config["ai_model"],
                                messages=[
                                    {"role": "system", "content": "你是一个严格的评分官"},
                                    {"role": "user", "content": prompt}
                                ],
                                temperature=0.3,
                                max_tokens=50,
                                timeout=active_config["ai_timeout"]
                            ),
                            timeout=active_config["ai_timeout"] + 5
                        )
                        
                        response_text = response.choices[0].message.content
                        
                        # 提取分数
                        score_match = re.search(r'(\d+)', response_text)
                        if score_match:
                            score = int(score_match.group(1))
                            # 验证分数范围
                            if 0 <= score <= total_score:
                                self.logger.info(f"AI评分结果: {score}分")
                                return score
                            else:
                                self.logger.warning(f"分数超出范围: {score}")
                                if attempt < max_retries - 1:
                                    await asyncio.sleep(2)
                                    continue
                        else:
                            self.logger.warning(f"无法解析分数: {response_text}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(2)
                                continue
                    
                except asyncio.TimeoutError:
                    self.logger.warning(f"评分请求超时 (尝试 {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
                except Exception as e:
                    self.logger.error(f"评分失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
            
            # 所有重试失败,返回0分
            self.logger.error("评分失败,返回0分")
            return 0
            
        except Exception as e:
            self.logger.error(f"评分异常: {e}", exc_info=True)
            return 0
    
    # ==================== 数据管理 ====================
    
    def _ensure_data_dir(self):
        """确保数据目录存在"""
        os.makedirs(self.DATA_DIR, exist_ok=True)
    
    def _load_config(self):
        """加载配置"""
        self.config = copy.deepcopy(self.DEFAULT_CONFIG)
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    self.config = self._deep_merge(self.config, loaded_config)
                self.logger.info("配置已加载")
            except Exception as e:
                self.logger.error(f"加载配置失败: {e}")
                self._save_config()
        else:
            self._save_config()
    
    def _save_config(self):
        """保存配置"""
        try:
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            self.logger.info("配置已保存")
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """深度合并配置，避免嵌套配置被浅拷贝污染。"""
        result = copy.deepcopy(base)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _config_for_server(self, target_server: Optional[Dict] = None,
                           server_key: Optional[str] = None) -> Dict:
        """读取目标服务器独立插件配置，找不到则回退根配置。"""
        config = self._deep_merge(self.DEFAULT_CONFIG, self.config or {})
        path = None
        try:
            if server_key and self.plugin_manager and hasattr(self.plugin_manager, "get_plugin_server_file_by_key"):
                path = self.plugin_manager.get_plugin_server_file_by_key(
                    "whitelist_audit", server_key, "config.json", create_parent=False
                )
            elif self.plugin_manager and hasattr(self.plugin_manager, "get_plugin_server_file"):
                path = self.plugin_manager.get_plugin_server_file(
                    "whitelist_audit", "config.json", target_server or {}, create_parent=False
                )
            if path and os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    config = self._deep_merge(config, loaded)
        except Exception as e:
            self.logger.error(f"读取服务器插件配置失败 {path}: {e}")
        return config

    def _save_config_for_server(self, config: Dict, target_server: Optional[Dict] = None,
                                server_key: Optional[str] = None):
        """保存目标服务器独立插件配置。"""
        path = None
        try:
            if server_key and self.plugin_manager and hasattr(self.plugin_manager, "get_plugin_server_file_by_key"):
                path = self.plugin_manager.get_plugin_server_file_by_key(
                    "whitelist_audit", server_key, "config.json", create_parent=True
                )
            elif self.plugin_manager and hasattr(self.plugin_manager, "get_plugin_server_file"):
                path = self.plugin_manager.get_plugin_server_file(
                    "whitelist_audit", "config.json", target_server or {}, create_parent=True
                )
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                self.logger.info(f"服务器配置已保存: {path}")
                return
        except Exception as e:
            self.logger.error(f"保存服务器插件配置失败 {path}: {e}")

        self.config = self._deep_merge(self.DEFAULT_CONFIG, config or {})
        self._save_config()

    def _session_config(self, session: Optional[Dict]) -> Dict:
        if isinstance(session, dict) and isinstance(session.get("config"), dict):
            return session["config"]
        if isinstance(session, dict):
            return self._config_for_server(server_key=session.get("server_key"))
        return self.config
    
    def _load_data(self):
        """加载数据"""
        """加载全局旧数据和每服务器独立数据。"""
        self.audit_records = {}
        self.whitelist = {}
        self.cooldown = {}

        server_files = self._server_data_files()
        has_server_data = any(
            os.path.exists(path)
            for paths in server_files.values()
            for path in paths.values()
        )
        if not has_server_data:
            self._merge_audit_records(self._read_json_file(self.AUDIT_RECORDS_FILE))
            self._merge_whitelist(self._read_json_file(self.WHITELIST_FILE))
            self._merge_cooldown(self._read_json_file(self.COOLDOWN_FILE))

        for server_key, paths in server_files.items():
            self._merge_audit_records(self._read_json_file(paths["audit_records"]), server_key)
            self._merge_whitelist(self._read_json_file(paths["whitelist"]), server_key)
            self._merge_cooldown(self._read_json_file(paths["cooldown"]), server_key)
    
    def _save_data(self):
        """按服务器拆分保存数据"""
        try:
            audit_records = self._group_audit_records()
            whitelist_data = self._group_whitelist()
            cooldown_data = self._group_cooldown()
            server_keys = set(self._server_data_files().keys())
            server_keys.update(audit_records.keys())
            server_keys.update(whitelist_data.keys())
            server_keys.update(cooldown_data.keys())

            for server_key in sorted(server_keys):
                with open(self._server_data_file(server_key, "audit_records.json"), 'w', encoding='utf-8') as f:
                    json.dump(audit_records.get(server_key, {}), f, ensure_ascii=False, indent=2)

            for server_key in sorted(server_keys):
                with open(self._server_data_file(server_key, "whitelist.json"), 'w', encoding='utf-8') as f:
                    json.dump(whitelist_data.get(server_key, {}), f, ensure_ascii=False, indent=2)

            for server_key in sorted(server_keys):
                with open(self._server_data_file(server_key, "cooldown.json"), 'w', encoding='utf-8') as f:
                    json.dump(cooldown_data.get(server_key, {}), f, ensure_ascii=False, indent=2)
            
            self.logger.info("数据已保存")
        except Exception as e:
            self.logger.error(f"保存数据失败: {e}")

    def _read_json_file(self, path):
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"读取数据文件失败 {path}: {e}")
        return {}

    def _merge_audit_records(self, incoming, server_key: Optional[str] = None):
        if not isinstance(incoming, dict):
            return
        seen = {
            (
                str(user_id),
                str(record.get("game_id")),
                str(record.get("audit_time") or record.get("start_time")),
                str(record.get("server_key", "default")),
            )
            for user_id, records in self.audit_records.items()
            for record in records
            if isinstance(record, dict)
        }
        for user_id, records in incoming.items():
            if not isinstance(records, list):
                continue
            target = self.audit_records.setdefault(str(user_id), [])
            for record in records:
                if not isinstance(record, dict):
                    continue
                item = dict(record)
                if server_key and not item.get("server_key"):
                    item["server_key"] = server_key
                key = (
                    str(user_id),
                    str(item.get("game_id")),
                    str(item.get("audit_time") or item.get("start_time")),
                    str(item.get("server_key", "default")),
                )
                if key in seen:
                    continue
                seen.add(key)
                target.append(item)

    def _merge_whitelist(self, incoming, server_key: Optional[str] = None):
        if not isinstance(incoming, dict):
            return
        for key, info in incoming.items():
            if not isinstance(info, dict):
                continue
            item = dict(info)
            if server_key and not item.get("server_key"):
                item["server_key"] = server_key
            entry_key = key
            if server_key and "::" not in str(entry_key):
                entry_key = f"{server_key}::{entry_key}"
            self.whitelist[str(entry_key)] = item

    def _merge_cooldown(self, incoming, server_key: Optional[str] = None):
        if not isinstance(incoming, dict):
            return
        for key, value in incoming.items():
            self.cooldown[str(key)] = value

    def _group_audit_records(self) -> Dict[str, Dict[str, List[Dict]]]:
        grouped: Dict[str, Dict[str, List[Dict]]] = {}
        for user_id, records in self.audit_records.items():
            for record in records:
                if not isinstance(record, dict):
                    continue
                server_key = str(record.get("server_key") or "default")
                grouped.setdefault(server_key, {}).setdefault(str(user_id), []).append(record)
        return grouped

    def _group_whitelist(self) -> Dict[str, Dict]:
        grouped: Dict[str, Dict] = {}
        for key, info in self.whitelist.items():
            server_key = self._entry_server_key(key, info)
            grouped.setdefault(server_key, {})[key] = info
        return grouped

    def _group_cooldown(self) -> Dict[str, Dict]:
        grouped: Dict[str, Dict] = {}
        for key, value in self.cooldown.items():
            grouped.setdefault(self._cooldown_server_key(key), {})[key] = value
        return grouped

    def _cooldown_server_key(self, key: str) -> str:
        text = str(key)
        if "::" in text:
            return text.split("::", 1)[0]
        parts = text.split("_", 2)
        return parts[1] if len(parts) >= 3 else "default"

    def _server_data_files(self) -> Dict[str, Dict[str, str]]:
        files = {}
        if not self.plugin_manager or not hasattr(self.plugin_manager, "get_configured_servers"):
            return files
        for server in self.plugin_manager.get_configured_servers():
            server_key = self._server_key(server)
            files[server_key] = {
                "audit_records": self._server_data_file(server_key, "audit_records.json"),
                "whitelist": self._server_data_file(server_key, "whitelist.json"),
                "cooldown": self._server_data_file(server_key, "cooldown.json"),
            }
        return files

    def _server_data_file(self, server_key: str, filename: str) -> str:
        if self.plugin_manager and hasattr(self.plugin_manager, "get_plugin_server_file_by_key"):
            return str(self.plugin_manager.get_plugin_server_file_by_key(
                "whitelist_audit", server_key, filename
            ))
        safe = self._safe_path_name(server_key)
        path = os.path.join(self.DATA_DIR, "servers", safe)
        os.makedirs(path, exist_ok=True)
        return os.path.join(path, filename)

    def _safe_path_name(self, value: str) -> str:
        text = str(value or "default").replace("\\", "/").strip()
        if "/" in text:
            text = os.path.splitext(os.path.basename(text))[0]
        safe = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in text)
        return safe.strip("._") or "default"
    
    def _save_audit_record(self, record: dict):
        """保存审核记录"""
        user_id = str(record["user_id"])
        if user_id not in self.audit_records:
            self.audit_records[user_id] = []
        
        self.audit_records[user_id].append(record)
        self._save_data()
    
    def _add_to_whitelist(self, game_id: str, user_id: int, group_id: int, admin: bool = False,
                          target_server: Optional[Dict] = None, server_key: Optional[str] = None):
        """添加到白名单"""
        server_key = server_key or self._server_key(target_server)
        self.whitelist[self._whitelist_key(game_id, server_key=server_key)] = {
            "game_id": game_id,
            "server_key": server_key,
            "user_id": user_id,
            "group_id": group_id,
            "added_by": "admin" if admin else "audit",
            "add_time": datetime.now().isoformat()
        }
        self._save_data()
        self.logger.info(f"游戏ID {game_id} 已添加到白名单")
    
    def _set_cooldown(self, user_id: int, game_id: str, target_server: Optional[Dict] = None,
                      server_key: Optional[str] = None, config: Optional[Dict] = None):
        """设置冷却时间"""
        active_config = config or self._config_for_server(target_server, server_key)
        key = self._cooldown_key(user_id, game_id, target_server, server_key)
        self.cooldown[key] = time.time() + active_config["cooldown_seconds"]
        self._save_data()
    
    def _check_cooldown(self, user_id: int, game_id: str, target_server: Optional[Dict] = None,
                        server_key: Optional[str] = None) -> int:
        """检查冷却时间"""
        key = self._cooldown_key(user_id, game_id, target_server, server_key)
        legacy_key = self._legacy_cooldown_key(user_id, game_id, target_server, server_key)
        active_key = key if key in self.cooldown else legacy_key if legacy_key in self.cooldown else key
        if active_key in self.cooldown:
            remaining = self.cooldown[active_key] - time.time()
            if remaining > 0:
                return int(remaining)
            else:
                del self.cooldown[active_key]
                self._save_data()
        return 0

    def _cooldown_key(self, user_id: int, game_id: str, target_server: Optional[Dict] = None,
                      server_key: Optional[str] = None) -> str:
        target_key = self._server_key(target_server) if server_key is None else server_key
        return f"{target_key}::{user_id}::{game_id}"

    def _legacy_cooldown_key(self, user_id: int, game_id: str, target_server: Optional[Dict] = None,
                             server_key: Optional[str] = None) -> str:
        target_key = self._server_key(target_server) if server_key is None else server_key
        return f"{user_id}_{target_key}_{game_id}"
    
    def _is_in_whitelist(self, game_id: str, target_server: Optional[Dict] = None,
                         server_key: Optional[str] = None) -> bool:
        """检查是否在白名单中"""
        return self._whitelist_key(game_id, target_server, server_key) in self.whitelist
    
    def _get_user_whitelist_count(self, user_id: int, target_server: Optional[Dict] = None,
                                  server_key: Optional[str] = None) -> int:
        """获取用户已绑定的白名单数量"""
        expected_server_key = self._server_key(target_server) if server_key is None else server_key
        count = 0
        for key, info in self.whitelist.items():
            if info["user_id"] == user_id and self._entry_server_key(key, info) == expected_server_key:
                count += 1
        return count
    
    def _is_group_allowed(self, group_id: int, target_server: Optional[Dict] = None) -> bool:
        """检查群组是否允许"""
        qq_groups = ((target_server or {}).get("qq") or {}).get("groups") or []
        if qq_groups:
            return group_id in qq_groups
        return group_id in self._config_for_server(target_server)["allowed_groups"]

    def _server_key(self, target_server: Optional[Dict] = None) -> str:
        target_server = target_server or {}
        return str(target_server.get("_config_file") or target_server.get("name") or "default")

    def _whitelist_key(self, game_id: str, target_server: Optional[Dict] = None,
                       server_key: Optional[str] = None) -> str:
        return f"{self._server_key(target_server) if server_key is None else server_key}::{game_id}"

    def _audit_key(self, game_id: str, server_key: str) -> str:
        return f"{server_key}::{game_id}"

    def _entry_server_key(self, key: str, info: Dict) -> str:
        if isinstance(info, dict) and info.get("server_key"):
            return str(info.get("server_key"))
        if "::" in str(key):
            return str(key).split("::", 1)[0]
        return "default"

    def _session_key(self, user_id: int, group_id: int, target_server: Optional[Dict] = None) -> str:
        return f"{user_id}_{group_id}_{self._server_key(target_server)}"
    
    def _is_valid_game_id(self, game_id: str) -> bool:
        """验证游戏ID格式"""
        return bool(re.match(r'^[a-zA-Z0-9_]{3,16}$', game_id))
