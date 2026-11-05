# 设置兼容清单

网页版沿用原桌面版的 `config.json` 结构。未在表单中使用的既有字段、服务商扩展字段及预设元数据会保留；保存、重命名或删除预设不会重新添加已删除的默认预设。

| 设置分组 | 原配置字段 / 操作 | 网页支持 |
| --- | --- | --- |
| LLM 预设 | `llm_configs`、`last_llm_config_name` | 选择、新增、复制、重命名、删除、保存、测试 |
| LLM 参数 | `interface_format`、`model_name`、`base_url`、`api_key`、`temperature`、`max_tokens`、`timeout` | 全部可编辑；保留原 Temperature 0–2、Max Tokens 0–102400、Timeout 0–3600 的范围，并支持更大的 Token / 超时上限 |
| 五项任务模型 | `choose_configs.architecture_llm`、`chapter_outline_llm`、`prompt_draft_llm`、`final_chapter_llm`、`consistency_review_llm` | 各自选择预设，重命名和删除时同步引用 |
| Embedding | `embedding_configs`、`last_embedding_interface_format` | 原配置按名称导入，支持选择、新增、复制、重命名、删除、保存、测试 |
| Embedding 参数 | `interface_format`、`model_name`、`base_url`、`api_key`、`retrieval_k` | 全部可编辑，测试返回向量维度 |
| 创作默认值 | `other_params` | 下列 11 项全部可编辑 |
| 创作语言 | 原中英文模式切换 | 保存为 `language`，后续任务使用对应语言的生成提示词 |
| 代理 | `proxy_setting.enabled`、`proxy_url`、`proxy_port` | 全部可编辑，后续模型与知识库请求使用对应代理快照 |
| WebDAV | `webdav_config.webdav_url`、`webdav_username`、`webdav_password` | 全部可编辑，支持测试、备份、恢复；恢复前保留本地备份 |
| 全局操作 | 加载配置、保存配置 | 全部支持，保存前检查配置版本 |
| 知识库 | 导入知识文件、清理向量库 | 支持 TXT / Markdown，保留源文件，可重新入库，清理后可补建章节索引 |

`other_params` 的 11 项为：`topic`（主题）、`genre`（类型）、`num_chapters`（章节数）、`word_number`（单章字数）、`filepath`（保存路径）、`chapter_num`（当前章节号）、`user_guidance`（创作指导）、`characters_involved`（人物）、`key_items`（道具）、`scene_location`（地点）、`time_constraint`（时间约束）。新建小说继承这些默认值，已有小说参数独立保存。

LLM 接口支持：OpenAI、Azure OpenAI、Azure AI、Ollama、DeepSeek、Gemini、ML Studio、阿里云百炼、火山引擎、硅基流动、Grok。该清单包含原桌面菜单和原适配器已经支持的格式。

Embedding 接口支持：OpenAI、Azure OpenAI、Gemini、Ollama、ML Studio、SiliconFlow。

现有 API Key 和 WebDAV 密码在页面中只显示已保存状态。留空保留、输入替换、明确勾选清除后删除。连接测试不会自动保存配置。WebDAV 备份保存磁盘中的完整配置，包括凭证；小说文本、向量库和网页任务数据库需另行备份。
