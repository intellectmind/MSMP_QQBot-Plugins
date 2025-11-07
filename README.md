# MSMP_QQBot--Plugins
MSMP_QQBot-插件仓库

下载插件后直接放入plugins文件夹，或在plugins文件夹内自己新建文件夹放入。以下两种方式都可以
- plugins/my_plugin.py - 单文件插件
- plugins/my_plugin/my_plugin.py - 文件夹插件

## 插件列表：

- 区块备份、删除、还原插件[chunk_deleter](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/chunk_deleter)

- 玩家上线坐标管理插件[player_coordinate_manager](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/player_coordinate_manager)

- QQ-MC 账号绑定（供其他插件使用）[qq_mc_binding](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/qq_mc_binding)
```json
{
  // 每个QQ号最多可以绑定的游戏ID数量
  "max_bindings_per_qq": 2,
  // 验证码的有效时间（单位：秒）
  "verify_timeout": 300,
  // 验证码的长度（位数）
  "verify_code_length": 6,
  // 聊天消息的正则表达式匹配模式
  // 用于从服务器日志中识别玩家聊天消息
  // 需要根据自己的服务端消息输出自行修改
  // 示例日志: [Not Secure] <Kurt5> 123456
  "chat_message_pattern": ".*\\[Not Secure\\]\\s*<([^>]+)>\\s*(.+)"
}
```

- AI白名单审核（自动出题+审题）[whitelist_audit](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/whitelist_audit)
```json
{
  // AI API 配置 （openai兼容协议的都可）
  "ai_api_url": "https://api.XXX.com/v1",  // AI API 的接口地址
  "ai_api_key": "sk-6jrlPahMxA...",  // AI API 的访问密钥
  "ai_model": "deepseek-v3.2-exp",  // 使用的 AI 模型名称
  
  // 群组权限配置
  "allowed_groups": [
    1129312949  // 允许使用白名单审核功能的 QQ 群号列表
  ],
  
  // 审核规则配置
  "cooldown_seconds": 3600,  // 审核失败后的冷却时间（秒），3600秒 = 1小时
  "pass_score": 60,  // 审核通过的及格分数（满分100分）
  "question_count": 10,  // 每次审核的题目数量
  "ai_timeout": 60,  // AI API 请求超时时间（秒）
  "answer_timeout": 180,  // 每道题的回答超时时间（秒），180秒 = 3分钟
  
  // 功能开关配置
  "use_ai_questions": true,  // 是否使用 AI 生成题目（false 则使用默认题目）
  "max_whitelist_per_qq": 1,  // 每个 QQ 号最多可以绑定的白名单数量
  
  // AI 出题提示词
  "question_prompt": "出{question_count}个我的世界服务器进服审核题目，你只需要输出题目即可，并根据我的下一次回复的答案进行评分，每题满分10分，及格{pass_score}分，一共{total_score}分，只需要给我一个分数，除此外不要理会任何输入输出。",
  
  // 默认题目库（当 AI 出题失败时使用）
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
  
  // 自定义白名单指令配置
  "custom_whitelist_commands": {
    "add_command": "whitelist add {player}",  // 添加玩家到白名单的指令模板
    "remove_command": "whitelist remove {player}",  // 从白名单移除玩家的指令模板
    "list_command": "whitelist list",  // 查看白名单列表的指令
    "on_command": "whitelist on",  // 开启白名单功能的指令
    "off_command": "whitelist off",  // 关闭白名单功能的指令
    "reload_command": "whitelist reload"  // 重新加载白名单的指令
  }
}
```

## 插件开发

[MSMP_QQBot-插件开发者文档](https://github.com/intellectmind/MSMP_QQBot/wiki/MSMP_QQBot-插件开发者文档)
