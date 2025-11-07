# MSMP_QQBot 插件仓库

## 安装方式

下载插件后，可采用以下两种方式安装：

| 安装方式 | 说明 |
|---------|------|
| **单文件插件** | `plugins/my_plugin.py` |
| **文件夹插件** | `plugins/my_plugin/my_plugin.py` |

---

## 可用插件列表

### 1. 区块备份、删除、还原插件
**插件名称：** `chunk_deleter`

管理服务器区块，支持备份、删除和还原功能。

[前往下载](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/chunk_deleter)

---

### 2. 玩家上线坐标管理插件
**插件名称：** `player_coordinate_manager`

记录和管理玩家上线时的坐标信息。

[前往下载](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/player_coordinate_manager)

---

### 3. QQ-MC 账号绑定插件
**插件名称：** `qq_mc_binding`

提供 QQ 与 Minecraft 账号绑定功能，供其他插件使用。

[前往下载](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/qq_mc_binding)

**配置示例：**

```json
{
  "max_bindings_per_qq": 2,
  "verify_timeout": 300,
  "verify_code_length": 6,
  "chat_message_pattern": ".*\\[Not Secure\\]\\s*<([^>]+)>\\s*(.+)"
}
```

| 配置项 | 说明 |
|--------|------|
| `max_bindings_per_qq` | 每个QQ号最多可绑定的游戏ID数量 |
| `verify_timeout` | 验证码有效时间（秒） |
| `verify_code_length` | 验证码长度（位数） |
| `chat_message_pattern` | 聊天消息正则表达式（需根据服务端消息自行修改） |

---

### 4. AI 白名单审核插件
**插件名称：** `whitelist_audit`

使用 AI 自动生成审核题目并自动评分，实现智能白名单审核。

[前往下载](https://github.com/intellectmind/MSMP_QQBot-Plugins/releases/tag/whitelist_audit)

**配置示例：**

```json
{
  "ai_api_url": "https://api.XXX.com/v1",
  "ai_api_key": "sk-6jrlPahMxA...",
  "ai_model": "deepseek-v3.2-exp",
  
  "allowed_groups": [1129312949],
  
  "cooldown_seconds": 3600,
  "pass_score": 60,
  "question_count": 10,
  "ai_timeout": 60,
  "answer_timeout": 180,
  
  "use_ai_questions": true,
  "max_whitelist_per_qq": 1,
  
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
  
  "custom_whitelist_commands": {
    "add_command": "whitelist add {player}",
    "remove_command": "whitelist remove {player}",
    "list_command": "whitelist list",
    "on_command": "whitelist on",
    "off_command": "whitelist off",
    "reload_command": "whitelist reload"
  }
}
```

**关键配置说明：**

| 配置项 | 说明 |
|--------|------|
| **AI 配置** | |
| `ai_api_url` | AI API 接口地址（支持 OpenAI 兼容协议） |
| `ai_api_key` | AI API 访问密钥 |
| `ai_model` | 使用的 AI 模型名称 |
| **权限配置** | |
| `allowed_groups` | 允许使用的 QQ 群号列表 |
| **审核规则** | |
| `cooldown_seconds` | 审核失败后的冷却时间（秒） |
| `pass_score` | 审核通过的及格分数（满分100分） |
| `question_count` | 每次审核的题目数量 |
| `ai_timeout` | AI API 请求超时时间（秒） |
| `answer_timeout` | 每道题的回答超时时间（秒） |
| **功能开关** | |
| `use_ai_questions` | 是否使用 AI 生成题目 |
| `max_whitelist_per_qq` | 每个 QQ 号最多可绑定的白名单数量 |

---

## 开发文档

想要开发自己的插件？查看完整的插件开发指南：

[MSMP_QQBot 插件开发者文档](https://github.com/intellectmind/MSMP_QQBot/wiki/MSMP_QQBot-插件开发者文档)
