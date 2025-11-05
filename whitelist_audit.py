import asyncio
import json
import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, List
from plugin_manager import BotPlugin
import aiohttp

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None


class WhitelistAuditPlugin(BotPlugin):
    """白名单审核插件 - 通过AI生成题目进行玩家审核"""
    
    name = "Whitelist Audit"
    version = "1.0.0"
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
        self.config = self.DEFAULT_CONFIG.copy()
        self.audit_records = {}
        self.whitelist = {}
        self.cooldown = {}
        self.audit_sessions = {}
        self.timeout_tasks = {}
        self.plugin_manager = None
        self.auditing_game_ids = set()  # 正在审核的游戏ID集合
        
    async def on_load(self, plugin_manager):
        """加载插件"""
        try:
            self._ensure_data_dir()
            self._load_config()
            self._load_data()
            self.plugin_manager = plugin_manager
            
            # 保存 QQBotServer 引用以便后续获取 RCON 客户端
            self.qq_bot_server = None
            if hasattr(plugin_manager, 'qq_bot_server'):
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
            asyncio.create_task(self._cleanup_expired_sessions())
            
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
                    if game_id in self.auditing_game_ids:
                        self.auditing_game_ids.remove(game_id)
                    
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
        for task_key in list(self.timeout_tasks.keys()):
            task = self.timeout_tasks[task_key]
            if not task.done():
                task.cancel()
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
            if not group_id:
                return "请在QQ群内申请白名单"
            
            if not self._is_group_allowed(group_id):
                return "此群组不支持白名单审核"
            
            game_id = command_text.strip()
            if not game_id:
                return "请输入游戏ID\n格式: 白名单审核 <游戏ID>"
            
            if not self._is_valid_game_id(game_id):
                return "游戏ID格式不正确\n要求: 3-16个字符，仅含字母、数字和下划线"
            
            # 检查冷却
            cooldown_remaining = self._check_cooldown(user_id, game_id)
            if cooldown_remaining > 0:
                hours = cooldown_remaining // 3600
                minutes = (cooldown_remaining % 3600) // 60
                return f"审核冷却中\n请在 {hours}小时{minutes}分钟后重试"
            
            # 检查是否已在白名单
            if self._is_in_whitelist(game_id):
                return f"游戏ID {game_id} 已在白名单中"
            
            # 检查用户已绑定的白名单数量
            user_whitelist_count = self._get_user_whitelist_count(user_id)
            max_allowed = self.config["max_whitelist_per_qq"]
            if user_whitelist_count >= max_allowed:
                return f"您已达到白名单绑定上限\n当前绑定: {user_whitelist_count}/{max_allowed}个\n如需绑定更多，请联系管理员"
            
            # 检查是否正在审核
            if game_id in self.auditing_game_ids:
                return f"游戏ID {game_id} 正在被其他用户审核中，请稍后再试"
            
            # 检查是否正在审核（用户会话）
            session_key = f"{user_id}_{group_id}"
            if session_key in self.audit_sessions:
                return "此ID正在审核中，请先完成当前审核"
            
            # ============ 立即返回确认消息 ============
            confirm_message = self._format_reply_with_at(user_id, 
                f"已收到白名单审核申请\n游戏ID: {game_id}\n正在准备题目，请稍候...")
            
            # 启动异步任务来准备题目和发送第一题
            asyncio.create_task(self._prepare_and_send_first_question(
                session_key, user_id, group_id, game_id, websocket
            ))
            
            self.logger.info(f"用户 {user_id} 开始审核准备，游戏ID: {game_id}")
            
            # 只返回确认消息，第一题会异步发送
            return confirm_message
        
        except Exception as e:
            self.logger.error(f"处理申请失败: {e}", exc_info=True)
            return f"处理失败: {str(e)}"

    async def _prepare_and_send_first_question(self, session_key, user_id, group_id, game_id, websocket):
        """异步准备题目并发送第一题"""
        try:
            # 获取题目
            questions = await self._fetch_questions()
            if not questions:
                error_msg = self._format_reply_with_at(user_id, "获取题目失败，请稍后重试")
                await self._send_group_message(websocket, group_id, error_msg)
                return
            
            # 创建审核会话
            self.audit_sessions[session_key] = {
                "user_id": user_id,
                "game_id": game_id,
                "group_id": group_id,
                "questions": questions,
                "answers": [],
                "current_question_index": 0,
                "start_time": datetime.now().isoformat(),
                "last_activity_time": time.time(),
                "current_question_start_time": time.time()
            }
            
            # 添加到正在审核的游戏ID集合
            self.auditing_game_ids.add(game_id)
            
            # 启动第一道题的超时任务
            timeout_task = asyncio.create_task(
                self._check_question_timeout(session_key, user_id, group_id, 0)
            )
            self.timeout_tasks[session_key] = timeout_task
            
            # 发送第一道题
            first_question = questions[0]
            timeout_minutes = self.config["answer_timeout"] // 60
            total_questions = self.config["question_count"]
            
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
            if websocket and not websocket.closed:
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

    async def handle_answer_command(self, user_id, group_id, command_text, websocket=None, **kwargs):
        """处理答案提交"""
        try:
            session_key = f"{user_id}_{group_id}"
            
            if session_key not in self.audit_sessions:
                return "没有正在进行的审核会话"
            
            answer = command_text.strip()
            if not answer:
                return "请输入答案\n格式: 答案 <你的答案>"
            
            if len(answer) > 500:
                return "答案过长，请简要回答"
            
            session = self.audit_sessions[session_key]
            
            # 检查当前题目是否超时
            current_question_elapsed = time.time() - session["current_question_start_time"]
            if current_question_elapsed > self.config["answer_timeout"]:
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
            total_questions = self.config["question_count"]
            
            self.logger.info(f"用户 {user_id} 提交第 {current_progress} 题答案: {answer[:50]}...")
            
            # 检查是否完成
            if current_progress >= total_questions:
                # 所有题目已回答,开始评分
                await self._complete_audit(session_key, user_id, group_id, websocket)
                return None  # 不返回消息，complete_audit会异步发送结果
            
            else:
                # 设置下一道题的开始时间
                session["current_question_start_time"] = time.time()
                
                # 发送下一道题
                next_index = current_progress
                if next_index < len(session["questions"]):
                    next_question = session["questions"][next_index]
                    timeout_minutes = self.config["answer_timeout"] // 60
                    
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
                session["answers"]
            )
            
            # 保存记录
            record = {
                "user_id": user_id,
                "game_id": game_id,
                "group_id": group_id,
                "questions": session["questions"],
                "answers": session["answers"],
                "score": score,
                "passed": score >= self.config["pass_score"],
                "start_time": session["start_time"],
                "end_time": datetime.now().isoformat()
            }
            
            self._save_audit_record(record)
            
            # 从正在审核的游戏ID集合中移除
            if game_id in self.auditing_game_ids:
                self.auditing_game_ids.remove(game_id)
            
            # 清除会话
            del self.audit_sessions[session_key]
            self._save_data()
            
            total_score = self.config["question_count"] * 10
            
            if score >= self.config["pass_score"]:
                # 审核通过，尝试添加到服务器白名单
                success = await self._add_to_server_whitelist(game_id, rcon_client)
                
                if success:
                    self._add_to_whitelist(game_id, user_id, group_id)
                    result_message = f"""恭喜！审核通过！
总分: {score}/{total_score}
游戏ID {game_id} 已加入服务器白名单
当前绑定: {self._get_user_whitelist_count(user_id)}/{self.config['max_whitelist_per_qq']}个"""
                else:
                    # RCON添加失败，只记录到插件白名单
                    self._add_to_whitelist(game_id, user_id, group_id)
                    result_message = f"""审核通过但服务器添加失败
总分: {score}/{total_score}
游戏ID {game_id} 已记录到插件白名单，但需要手动添加到服务器
当前绑定: {self._get_user_whitelist_count(user_id)}/{self.config['max_whitelist_per_qq']}个"""
            else:
                self._set_cooldown(user_id, game_id)
                result_message = f"""未通过审核
得分: {score}/{total_score}（及格线: {self.config['pass_score']}分）
请在 {self.config['cooldown_seconds']//3600} 小时后重试"""
            
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
            await asyncio.sleep(self.config["answer_timeout"])
            
            # 添加会话有效性检查
            if session_key not in self.audit_sessions:
                return
                
            session = self.audit_sessions[session_key]
            
            # 检查题目索引是否匹配(防止旧任务误触发)
            if len(session["answers"]) != question_index:
                return
            
            # 添加游戏ID检查
            game_id = session.get("game_id")
            if game_id not in self.auditing_game_ids:
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
        game_id = session["game_id"]
        
        # 只处理当前题目的超时（防止旧任务误触发）
        if len(session["answers"]) != question_index:
            return
        
        # 记录超时的题目（用空字符串表示超时未答）
        while len(session["answers"]) <= question_index:
            session["answers"].append("")  # 超时未答
        
        # 从正在审核的游戏ID集合中移除
        if game_id in self.auditing_game_ids:
            self.auditing_game_ids.remove(game_id)
        
        # 保存记录
        record = {
            "user_id": user_id,
            "game_id": game_id,
            "group_id": group_id,
            "questions": session["questions"],
            "answers": session["answers"],
            "score": 0,
            "passed": False,
            "state": f"timeout_question_{question_index + 1}",
            "start_time": session["start_time"],
            "end_time": datetime.now().isoformat()
        }
        
        self._save_audit_record(record)
        self._set_cooldown(user_id, game_id)
        
        # 清除任务
        if session_key in self.timeout_tasks:
            del self.timeout_tasks[session_key]
        del self.audit_sessions[session_key]
        
        self._save_data()
        self.logger.warning(f"用户 {user_id} 第{question_index + 1}题超时")
    
    async def _add_to_server_whitelist(self, game_id: str, rcon_client=None) -> bool:
        """通过RCON将游戏ID添加到服务器白名单"""
        try:
            # 检查 RCON 客户端是否可用
            if not rcon_client:
                self.logger.debug("RCON 客户端不可用")
                return False
            
            if not rcon_client.is_connected():
                self.logger.debug("RCON 连接不可用")
                return False
            
            # 使用自定义指令格式
            command_template = self.config["custom_whitelist_commands"]["add_command"]
            command = command_template.format(player=game_id)
            
            self.logger.info(f"通过RCON执行命令: {command}")
            
            result = rcon_client.execute_command(command)
            self.logger.info(f"RCON执行结果: {result}")
            
            # 检查执行结果 - 放宽条件，只要不是 None 就认为成功
            if result is not None:
                self.logger.info(f"成功将 {game_id} 添加到服务器白名单")
                return True
            else:
                self.logger.warning(f"RCON添加白名单失败: 返回None")
                # 即使返回None，也尝试检查是否真的添加成功
                return await self._check_whitelist_status(game_id, rcon_client)
                
        except Exception as e:
            self.logger.error(f"通过RCON添加白名单失败: {e}")
            return False

    async def _check_whitelist_status(self, game_id: str, rcon_client) -> bool:
        """检查玩家是否在白名单中"""
        try:
            command_template = self.config["custom_whitelist_commands"]["list_command"]
            result = rcon_client.execute_command(command_template)
            
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
            session_key = f"{user_id}_{group_id}"
            
            if session_key in self.audit_sessions:
                session = self.audit_sessions[session_key]
                progress = len(session["answers"])
                total = self.config["question_count"]
                game_id = session["game_id"]
                
                # 计算当前题目的剩余时间
                current_question_elapsed = time.time() - session["current_question_start_time"]
                remaining_time = max(0, self.config["answer_timeout"] - current_question_elapsed)
                minutes = int(remaining_time // 60)
                seconds = int(remaining_time % 60)
                
                status_message = f"""审核进度
游戏ID: {game_id}
进度: {progress}/{total}
当前状态: {"答题中" if progress < total else "已完成"}
当前题目剩余时间: {minutes}分{seconds}秒
提交答案请使用: 答案 <你的答案>"""
                
                return self._format_reply_with_at(user_id, status_message)
            
            user_records = self.audit_records.get(str(user_id), [])
            if user_records:
                latest = user_records[-1]
                status = "已通过" if latest["passed"] else "未通过"
                user_whitelist_count = self._get_user_whitelist_count(user_id)
                max_allowed = self.config["max_whitelist_per_qq"]
                
                total_score = self.config["question_count"] * 10
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
            if not self.whitelist:
                return "白名单为空"
            
            lines = ["=== 服务器白名单 ==="]
            for i, (game_id, info) in enumerate(list(self.whitelist.items())[:20], 1):
                lines.append(f"{i}. {game_id}")
            
            if len(self.whitelist) > 20:
                lines.append(f"\n... 还有 {len(self.whitelist) - 20} 个玩家")
            
            return "\n".join(lines)
        
        except Exception as e:
            self.logger.error(f"查看白名单失败: {e}", exc_info=True)
            return "查看失败"
    
    async def handle_admin(self, user_id, group_id, command_text, rcon_client=None, **kwargs):
        """管理员操作"""
        try:
            parts = command_text.strip().split()
            if not parts:
                return "子命令: add <游戏ID> | remove <游戏ID> | clear | reload | sessions | reset <用户ID> | sync | config | set_max <QQ号> <数量> | set_command <类型> <指令>"
            
            action = parts[0]
            
            if action == "add" and len(parts) > 1:
                game_id = parts[1]
                # 先尝试通过RCON添加到服务器
                success = await self._add_to_server_whitelist(game_id, rcon_client)
                if success:
                    self._add_to_whitelist(game_id, user_id, group_id, admin=True)
                    return f"已将 {game_id} 加入服务器白名单"
                else:
                    # RCON失败，只添加到插件白名单
                    self._add_to_whitelist(game_id, user_id, group_id, admin=True)
                    return f"已将 {game_id} 加入插件白名单，但服务器添加失败，请手动处理"
            
            elif action == "remove" and len(parts) > 1:
                game_id = parts[1]
                if game_id in self.whitelist:
                    # 同时从服务器白名单移除
                    success = await self._remove_from_server_whitelist(game_id, rcon_client)
                    del self.whitelist[game_id]
                    self._save_data()
                    if success:
                        return f"已从服务器和插件白名单中移出 {game_id}"
                    else:
                        return f"已从插件白名单中移出 {game_id}，但服务器移除失败，请手动处理"
                return "未找到该游戏ID"
            
            elif action == "clear":
                # 清空插件白名单
                self.whitelist.clear()
                self._save_data()
                return "已清空白名单"
            
            elif action == "reload":
                self._load_data()
                return "已重新加载"
            
            elif action == "sessions":
                # 查看当前活跃会话
                if not self.audit_sessions:
                    return "当前无活跃审核会话"
                
                lines = ["当前审核会话:"]
                for key, session in self.audit_sessions.items():
                    progress = len(session["answers"])
                    total = self.config["question_count"]
                    elapsed = int(time.time() - session["current_question_start_time"])
                    remaining = max(0, self.config["answer_timeout"] - elapsed)
                    minutes = int(remaining // 60)
                    seconds = int(remaining % 60)
                    lines.append(f"- {session['game_id']}: {progress}/{total}题 (剩余: {minutes}分{seconds}秒)")
                
                return "\n".join(lines)
            
            elif action == "reset" and len(parts) > 1:
                # 重置用户会话
                target_user_id = parts[1]
                session_key_to_remove = None
                for key in self.audit_sessions.keys():
                    if key.startswith(f"{target_user_id}_"):
                        session_key_to_remove = key
                        break
                
                if session_key_to_remove:
                    session = self.audit_sessions[session_key_to_remove]
                    game_id = session["game_id"]
                    
                    # 从正在审核的游戏ID集合中移除
                    if game_id in self.auditing_game_ids:
                        self.auditing_game_ids.remove(game_id)
                    
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
                
                for game_id in list(self.whitelist.keys()):
                    success = await self._add_to_server_whitelist(game_id, rcon_client)
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
                total_score = self.config["question_count"] * 10
                config_info = [
                    "当前配置:",
                    f"AI出题: {'开启' if self.config['use_ai_questions'] else '关闭'}",
                    f"题目数量: {self.config['question_count']}题",
                    f"总分: {total_score}分",
                    f"及格分数: {self.config['pass_score']}分",
                    f"答题超时: {self.config['answer_timeout']//60}分钟",
                    f"冷却时间: {self.config['cooldown_seconds']//3600}小时",
                    f"每个QQ号最大绑定: {self.config['max_whitelist_per_qq']}个",
                    f"允许群组: {len(self.config['allowed_groups'])}个",
                    "",
                    "自定义白名单指令:",
                    f"添加: {self.config['custom_whitelist_commands']['add_command']}",
                    f"移除: {self.config['custom_whitelist_commands']['remove_command']}",
                    f"列表: {self.config['custom_whitelist_commands']['list_command']}",
                    f"开启: {self.config['custom_whitelist_commands']['on_command']}",
                    f"关闭: {self.config['custom_whitelist_commands']['off_command']}",
                    f"重载: {self.config['custom_whitelist_commands']['reload_command']}"
                ]
                return "\n".join(config_info)
            
            elif action == "set_max" and len(parts) > 2:
                """设置QQ号的最大白名单数量"""
                try:
                    target_qq = parts[1]
                    new_max = int(parts[2])
                    
                    if new_max < 1:
                        return "最大绑定数量必须大于0"
                    
                    old_max = self.config["max_whitelist_per_qq"]
                    self.config["max_whitelist_per_qq"] = new_max
                    self._save_config()
                    
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
                
                old_command = self.config["custom_whitelist_commands"][f"{command_type}_command"]
                self.config["custom_whitelist_commands"][f"{command_type}_command"] = new_command
                self._save_config()
                
                return f"已更新 {command_type} 指令:\n旧: {old_command}\n新: {new_command}"
            
            else:
                return "未知操作"
        
        except Exception as e:
            self.logger.error(f"管理操作失败: {e}", exc_info=True)
            return "操作失败"
    
    async def _remove_from_server_whitelist(self, game_id: str, rcon_client=None) -> bool:
        """通过RCON从服务器白名单移除游戏ID"""
        try:
            # 检查 RCON 客户端是否可用
            if not rcon_client:
                self.logger.debug("RCON 客户端不可用")
                return False
            
            if not rcon_client.is_connected():
                self.logger.debug("RCON 连接不可用")
                return False
            
            # 使用自定义指令格式
            command_template = self.config["custom_whitelist_commands"]["remove_command"]
            command = command_template.format(player=game_id)
            
            self.logger.info(f"通过RCON执行移除命令: {command}")
            
            result = rcon_client.execute_command(command)
            self.logger.info(f"RCON移除结果: {result}")
            
            # 检查执行结果 - 放宽条件，只要不是 None 就认为成功
            if result is not None:
                self.logger.info(f"成功将 {game_id} 从服务器白名单移除")
                return True
            else:
                self.logger.warning(f"RCON移除白名单失败: 返回None")
                # 即使返回None，也尝试检查是否真的移除了
                return await self._check_whitelist_removed(game_id, rcon_client)
                
        except Exception as e:
            self.logger.error(f"通过RCON移除白名单失败: {e}")
            return False

    async def _check_whitelist_removed(self, game_id: str, rcon_client) -> bool:
        """检查玩家是否已从白名单中移除"""
        try:
            command_template = self.config["custom_whitelist_commands"]["list_command"]
            result = rcon_client.execute_command(command_template)
            
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
    
    async def _fetch_questions(self) -> Optional[List[str]]:
        """获取题目"""
        if self.config["use_ai_questions"]:
            questions = await self._fetch_questions_from_ai()
            if questions and len(questions) >= self.config["question_count"]:
                return questions[:self.config["question_count"]]
        
        # 使用默认题目
        return self._get_default_questions()
    
    async def _fetch_questions_from_ai(self) -> Optional[List[str]]:
        """从AI获取题目 - 带重试机制"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # 动态生成提示词
                total_score = self.config["question_count"] * 10
                prompt = self.config["question_prompt"].format(
                    question_count=self.config["question_count"],
                    pass_score=self.config["pass_score"],
                    total_score=total_score
                )
                
                if AsyncOpenAI:
                    client = AsyncOpenAI(
                        api_key=self.config['ai_api_key'],
                        base_url=self.config['ai_api_url']
                    )
                    
                    response = await client.chat.completions.create(
                        model=self.config["ai_model"],
                        messages=[
                            {"role": "system", "content": "你是一个我的世界服务器审核出题官"},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0.7,
                        max_tokens=2000,
                        timeout=self.config["ai_timeout"]
                    )
                    
                    response_text = response.choices[0].message.content
                    
                    # 解析题目
                    questions = self._parse_questions(response_text)
                    
                    if len(questions) >= self.config["question_count"]:
                        self.logger.info(f"成功从AI获取 {len(questions)} 道题目")
                        return questions
                    else:
                        self.logger.warning(f"AI出题数量不足: {len(questions)}/{self.config['question_count']}")
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
    
    def _get_default_questions(self) -> List[str]:
        """从默认题目中随机抽取"""
        import random
        default_questions = self.config["default_questions"]
        question_count = self.config["question_count"]
        
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
    
    async def _evaluate_answers(self, questions: List[str], answers: List[str]) -> int:
        """评分 - 优化版本"""
        try:
            # 检查是否有空答案(超时未答)
            empty_count = sum(1 for a in answers if not a.strip())
            if empty_count > 0:
                self.logger.info(f"检测到 {empty_count} 道题目未作答")
            
            qa_text = ""
            for i, (q, a) in enumerate(zip(questions, answers), 1):
                answer_text = a if a.strip() else "[未作答]"
                qa_text += f"{i}. 题目: {q}\n   答案: {answer_text}\n\n"
            
            total_score = self.config["question_count"] * 10
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
                            api_key=self.config['ai_api_key'],
                            base_url=self.config['ai_api_url']
                        )
                        
                        response = await asyncio.wait_for(
                            client.chat.completions.create(
                                model=self.config["ai_model"],
                                messages=[
                                    {"role": "system", "content": "你是一个严格的评分官"},
                                    {"role": "user", "content": prompt}
                                ],
                                temperature=0.3,
                                max_tokens=50,
                                timeout=self.config["ai_timeout"]
                            ),
                            timeout=self.config["ai_timeout"] + 5
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
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    self.config.update(loaded_config)
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
    
    def _load_data(self):
        """加载数据"""
        # 加载审核记录
        if os.path.exists(self.AUDIT_RECORDS_FILE):
            try:
                with open(self.AUDIT_RECORDS_FILE, 'r', encoding='utf-8') as f:
                    self.audit_records = json.load(f)
            except Exception as e:
                self.logger.error(f"加载审核记录失败: {e}")
                self.audit_records = {}
        
        # 加载白名单
        if os.path.exists(self.WHITELIST_FILE):
            try:
                with open(self.WHITELIST_FILE, 'r', encoding='utf-8') as f:
                    self.whitelist = json.load(f)
            except Exception as e:
                self.logger.error(f"加载白名单失败: {e}")
                self.whitelist = {}
        
        # 加载冷却数据
        if os.path.exists(self.COOLDOWN_FILE):
            try:
                with open(self.COOLDOWN_FILE, 'r', encoding='utf-8') as f:
                    self.cooldown = json.load(f)
            except Exception as e:
                self.logger.error(f"加载冷却数据失败: {e}")
                self.cooldown = {}
    
    def _save_data(self):
        """保存数据"""
        try:
            with open(self.AUDIT_RECORDS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.audit_records, f, ensure_ascii=False, indent=2)
            
            with open(self.WHITELIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.whitelist, f, ensure_ascii=False, indent=2)
            
            with open(self.COOLDOWN_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.cooldown, f, ensure_ascii=False, indent=2)
            
            self.logger.info("数据已保存")
        except Exception as e:
            self.logger.error(f"保存数据失败: {e}")
    
    def _save_audit_record(self, record: dict):
        """保存审核记录"""
        user_id = str(record["user_id"])
        if user_id not in self.audit_records:
            self.audit_records[user_id] = []
        
        self.audit_records[user_id].append(record)
        self._save_data()
    
    def _add_to_whitelist(self, game_id: str, user_id: int, group_id: int, admin: bool = False):
        """添加到白名单"""
        self.whitelist[game_id] = {
            "user_id": user_id,
            "group_id": group_id,
            "added_by": "admin" if admin else "audit",
            "add_time": datetime.now().isoformat()
        }
        self._save_data()
        self.logger.info(f"游戏ID {game_id} 已添加到白名单")
    
    def _set_cooldown(self, user_id: int, game_id: str):
        """设置冷却时间"""
        key = f"{user_id}_{game_id}"
        self.cooldown[key] = time.time() + self.config["cooldown_seconds"]
        self._save_data()
    
    def _check_cooldown(self, user_id: int, game_id: str) -> int:
        """检查冷却时间"""
        key = f"{user_id}_{game_id}"
        if key in self.cooldown:
            remaining = self.cooldown[key] - time.time()
            if remaining > 0:
                return int(remaining)
            else:
                del self.cooldown[key]
                self._save_data()
        return 0
    
    def _is_in_whitelist(self, game_id: str) -> bool:
        """检查是否在白名单中"""
        return game_id in self.whitelist
    
    def _get_user_whitelist_count(self, user_id: int) -> int:
        """获取用户已绑定的白名单数量"""
        count = 0
        for info in self.whitelist.values():
            if info["user_id"] == user_id:
                count += 1
        return count
    
    def _is_group_allowed(self, group_id: int) -> bool:
        """检查群组是否允许"""
        return group_id in self.config["allowed_groups"]
    
    def _is_valid_game_id(self, game_id: str) -> bool:
        """验证游戏ID格式"""
        return bool(re.match(r'^[a-zA-Z0-9_]{3,16}$', game_id))