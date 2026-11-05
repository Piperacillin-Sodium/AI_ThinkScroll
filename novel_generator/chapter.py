# novel_generator/chapter.py
# -*- coding: utf-8 -*-
"""
章节草稿生成及获取历史章节文本、当前章节摘要等
"""
import os
import json
import logging
import re  # 添加re模块导入
from llm_adapters import create_llm_adapter
import prompt_definitions
from chapter_directory_parser import get_chapter_info_from_blueprint
from novel_generator.common import invoke_with_cleaning
from utils import read_file, clear_file_content, save_string_to_txt
from novel_generator.vectorstore_utils import (
    get_relevant_context_from_vector_store,
    load_vector_store  # 添加导入
)
logging.basicConfig(
    filename='app.log',      # 日志文件名
    filemode='a',            # 追加模式（'w' 会覆盖）
    level=logging.INFO,      # 记录 INFO 及以上级别的日志
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def get_last_n_chapters_text(chapters_dir: str, current_chapter_num: int, n: int = 3) -> list:
    """
    从目录 chapters_dir 中获取最近 n 章的文本内容，返回文本列表。
    """
    texts = []
    start_chap = max(1, current_chapter_num - n)
    for c in range(start_chap, current_chapter_num):
        chap_file = os.path.join(chapters_dir, f"chapter_{c}.txt")
        if os.path.exists(chap_file):
            text = read_file(chap_file).strip()
            texts.append(text)
        else:
            texts.append("")
    return texts

def summarize_recent_chapters(
    interface_format: str,
    api_key: str,
    base_url: str,
    model_name: str,
    temperature: float,
    max_tokens: int,
    chapters_text_list: list,
    novel_number: int,            # 新增参数
    chapter_info: dict,           # 新增参数
    next_chapter_info: dict,      # 新增参数
    timeout: int = 600
) -> str:  # 修改返回值类型为 str，不再是 tuple
    """
    根据前三章内容生成当前章节的精准摘要。
    如果解析失败，则返回空字符串。
    """
    try:
        combined_text = "\n".join(chapters_text_list).strip()
        if not combined_text:
            return ""
            
        # 限制组合文本长度
        max_combined_length = 4000
        if len(combined_text) > max_combined_length:
            combined_text = combined_text[-max_combined_length:]
            
        llm_adapter = create_llm_adapter(
            interface_format=interface_format,
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout
        )
        
        # 确保所有参数都有默认值
        chapter_info = chapter_info or {}
        next_chapter_info = next_chapter_info or {}
        
        prompt = prompt_definitions.summarize_recent_chapters_prompt.format(
            combined_text=combined_text,
            novel_number=novel_number,
            chapter_title=chapter_info.get("chapter_title", "未命名"),
            chapter_role=chapter_info.get("chapter_role", "常规章节"),
            chapter_purpose=chapter_info.get("chapter_purpose", "内容推进"),
            suspense_level=chapter_info.get("suspense_level", "中等"),
            foreshadowing=chapter_info.get("foreshadowing", "无"),
            plot_twist_level=chapter_info.get("plot_twist_level", "★☆☆☆☆"),
            chapter_summary=chapter_info.get("chapter_summary", ""),
            next_chapter_number=novel_number + 1,
            next_chapter_title=next_chapter_info.get("chapter_title", "（未命名）"),
            next_chapter_role=next_chapter_info.get("chapter_role", "过渡章节"),
            next_chapter_purpose=next_chapter_info.get("chapter_purpose", "承上启下"),
            next_chapter_summary=next_chapter_info.get("chapter_summary", "衔接过渡内容"),
            next_chapter_suspense_level=next_chapter_info.get("suspense_level", "中等"),
            next_chapter_foreshadowing=next_chapter_info.get("foreshadowing", "无特殊伏笔"),
            next_chapter_plot_twist_level=next_chapter_info.get("plot_twist_level", "★☆☆☆☆")
        )
        
        response_text = invoke_with_cleaning(llm_adapter, prompt)
        summary = extract_summary_from_response(response_text)
        
        if not summary:
            logging.warning("Failed to extract summary, using full response")
            return response_text[:2000]  # 限制长度
            
        return summary[:2000]  # 限制摘要长度
        
    except Exception as e:
        logging.error(f"Error in summarize_recent_chapters: {str(e)}")
        return ""

def extract_summary_from_response(response_text: str) -> str:
    """从响应文本中提取摘要部分"""
    if not response_text:
        return ""
        
    # 查找摘要标记
    summary_markers = [
        "Current Chapter Summary:",
        "Summary:",
        "当前章节摘要:", 
        "章节摘要:",
        "摘要:",
        "本章摘要:"
    ]
    
    for marker in summary_markers:
        if (marker in response_text):
            parts = response_text.split(marker, 1)
            if len(parts) > 1:
                return parts[1].strip()
    
    return response_text.strip()

def format_chapter_info(chapter_info: dict) -> str:
    """将章节信息字典格式化为文本"""
    template = """
章节编号：第{number}章
章节标题：《{title}》
章节定位：{role}
核心作用：{purpose}
主要人物：{characters}
关键道具：{items}
场景地点：{location}
伏笔设计：{foreshadow}
悬念密度：{suspense}
转折程度：{twist}
章节简述：{summary}
"""
    return template.format(
        number=chapter_info.get('chapter_number', '未知'),
        title=chapter_info.get('chapter_title', '未知'),
        role=chapter_info.get('chapter_role', '未知'),
        purpose=chapter_info.get('chapter_purpose', '未知'),
        characters=chapter_info.get('characters_involved', '未指定'),
        items=chapter_info.get('key_items', '未指定'),
        location=chapter_info.get('scene_location', '未指定'),
        foreshadow=chapter_info.get('foreshadowing', '无'),
        suspense=chapter_info.get('suspense_level', '一般'),
        twist=chapter_info.get('plot_twist_level', '★☆☆☆☆'),
        summary=chapter_info.get('chapter_summary', '未提供')
    )


def generate_chapter_scene_plan(
    interface_format: str,
    api_key: str,
    base_url: str,
    model_name: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    **context
) -> str:
    """正文生成前先形成可执行的场景顺序和连续性约束。"""
    llm_adapter = create_llm_adapter(
        interface_format=interface_format,
        base_url=base_url,
        model_name=model_name,
        api_key=api_key,
        temperature=min(temperature, 0.4),
        # 思考型模型可能把输出预算用于内部推理；沿用用户配置的上限，
        # 避免 final content 尚未生成就耗尽 max_tokens。
        max_tokens=max(1, max_tokens),
        timeout=timeout
    )
    prompt = prompt_definitions.chapter_scene_plan_prompt.format(**context)
    chapter_label = context.get("novel_number", "未知")
    try:
        plan = invoke_with_cleaning(llm_adapter, prompt)
    except Exception as e:
        logging.exception("Scene plan generation failed for chapter %s", chapter_label)
        raise RuntimeError(f"第{chapter_label}章场景计划生成失败：{e}") from e

    if not plan.strip():
        raise RuntimeError(f"第{chapter_label}章场景计划为空，无法继续生成正文。")

    required_sections = (
        "1. 本章起点",
        "2. 场景顺序",
        "3. 本章转折",
        "4. 本章终点",
        "5. 必须保持的事实",
        "6. 连续性风险",
    )
    section_heading = re.compile(
        r"(?m)^\s*(?:[#>*\-]\s*)?(\d+)[.、．:：]\s*"
        r"(本章起点|场景顺序|本章转折|本章终点|必须保持的事实|连续性风险)\s*[：:]?"
    )

    def find_missing_sections(text: str) -> list:
        # 检查标题、内容和句子是否收尾；“无”是有效的明确回答。
        matches = list(section_heading.finditer(text))
        found = {}
        for index, match in enumerate(matches):
            title = f"{match.group(1)}. {match.group(2)}"
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end():end].strip()
            is_explicitly_empty = body.rstrip(" \t\r\n。！？；") == "无"
            has_sentence_ending = body.rstrip().endswith(
                ("。", "！", "？", "；", "…", "”", "’", "）", ")")
            )
            if body and (is_explicitly_empty or has_sentence_ending):
                found[title] = body
        return [section for section in required_sections if section not in found]

    missing_sections = find_missing_sections(plan)
    for repair_round in range(1, 3):
        if not missing_sections:
            break
        logging.warning(
            "Incomplete scene plan for chapter %s; requesting missing sections only (round %s): %s",
            chapter_label, repair_round, "、".join(missing_sections)
        )
        # 仅传入缺失项、当前计划和本章核心要求，避免重复发送完整架构与历史上下文。
        missing_titles = "\n".join(f"- {section}" for section in missing_sections)
        repair_prompt = f"""你是小说场景计划编辑。请为第{chapter_label}章《{context.get('chapter_title', '')}》补齐计划中缺失的部分。

【本章目录简述】
{context.get('chapter_summary', '未提供')}
【本章定位与核心作用】
{context.get('chapter_role', '未提供')}；{context.get('chapter_purpose', '未提供')}
【必须遵守的已知事实】
{context.get('previous_chapter_excerpt', '无前章内容')}
{context.get('character_state', '未提供角色状态')}

【已有计划，仅供衔接，不要重写】
{plan}

【只输出以下缺失部分】
{missing_titles}

每部分至少写一句具体内容；无相关内容时明确写“无”。保持与已有计划一致，不得引入无依据的重大事实，不要重复输出已有部分。"""
        try:
            repaired_sections = invoke_with_cleaning(llm_adapter, repair_prompt)
            if repaired_sections.strip():
                plan = plan.rstrip() + "\n" + repaired_sections.strip()
        except Exception as e:
            logging.warning(
                "Scene-plan completion round %s failed for chapter %s: %s",
                repair_round, chapter_label, e
            )
        missing_sections = find_missing_sections(plan)

    if missing_sections:
        logging.error(
            "Scene plan for chapter %s remains incomplete after repair; missing sections: %s",
            chapter_label, "、".join(missing_sections)
        )
        raise RuntimeError(
            f"第{chapter_label}章场景计划未达要求，缺少：{'、'.join(missing_sections)}。"
            "请查看输出日志后重试。"
        )
    logging.info("Chapter scene plan generated for chapter %s", context.get("novel_number"))
    return plan

def parse_search_keywords(response_text: str) -> list:
    """解析新版关键词格式（示例输入：'科技公司·数据泄露\n地下实验室·基因编辑'）"""
    return [
        line.strip().replace('·', ' ')
        for line in response_text.strip().split('\n')
        if '·' in line
    ][:5]  # 最多取5组

def apply_content_rules(texts: list, novel_number: int) -> list:
    """应用内容处理规则"""
    processed = []
    for text in texts:
        if re.search(r'第[\d]+章', text) or re.search(r'chapter_[\d]+', text):
            chap_nums = list(map(int, re.findall(r'\d+', text)))
            recent_chap = max(chap_nums) if chap_nums else 0
            time_distance = novel_number - recent_chap
            
            if time_distance <= 2:
                processed.append(f"[SKIP] 跳过近章内容：{text[:120]}...")
            elif 3 <= time_distance <= 5:
                processed.append(f"[MOD40%] {text}（需修改≥40%）")
            else:
                processed.append(f"[OK] {text}（可引用核心）")
        else:
            processed.append(f"[PRIOR] {text}（优先使用）")
    return processed

def apply_knowledge_rules(contexts: list, chapter_num: int) -> list:
    """应用知识库使用规则"""
    processed = []
    for text in contexts:
        # 检测历史章节内容
        if "第" in text and "章" in text:
            # 提取章节号判断时间远近
            chap_nums = [int(s) for s in text.split() if s.isdigit()]
            recent_chap = max(chap_nums) if chap_nums else 0
            time_distance = chapter_num - recent_chap
            
            # 相似度处理规则
            if time_distance <= 3:  # 近三章内容
                processed.append(f"[历史章节限制] 跳过近期内容: {text[:50]}...")
                continue
                
            # 允许引用但需要转换
            processed.append(f"[历史参考] {text} (需进行30%以上改写)")
        else:
            # 第三方知识优先处理
            processed.append(f"[外部知识] {text}")
    return processed

def get_filtered_knowledge_context(
    api_key: str,
    base_url: str,
    model_name: str,
    interface_format: str,
    embedding_adapter,
    filepath: str,
    chapter_info: dict,
    retrieved_texts: list,
    max_tokens: int = 2048,
    timeout: int = 600
) -> str:
    """优化后的知识过滤处理"""
    if not retrieved_texts:
        return "（无相关知识库内容）"

    try:
        processed_texts = apply_knowledge_rules(retrieved_texts, chapter_info.get('chapter_number', 0))
        llm_adapter = create_llm_adapter(
            interface_format=interface_format,
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout
        )
        
        # 限制检索文本长度并格式化
        formatted_texts = []
        max_text_length = 600
        for i, text in enumerate(processed_texts, 1):
            if len(text) > max_text_length:
                text = text[:max_text_length] + "..."
            formatted_texts.append(f"[预处理结果{i}]\n{text}")

        # 使用格式化函数处理章节信息
        import config_manager
        if config_manager.IS_ENGLISH:
            formatted_chapter_info = (
                f"Current chapter role: {chapter_info.get('chapter_role', '')}\n"
                f"Core goal: {chapter_info.get('chapter_purpose', '')}\n"
                f"Key elements: {chapter_info.get('characters_involved', '')} | "
                f"{chapter_info.get('key_items', '')} | "
                f"{chapter_info.get('scene_location', '')}"
            )
        else:
            formatted_chapter_info = (
                f"当前章节定位：{chapter_info.get('chapter_role', '')}\n"
                f"核心目标：{chapter_info.get('chapter_purpose', '')}\n"
                f"关键要素：{chapter_info.get('characters_involved', '')} | "
                f"{chapter_info.get('key_items', '')} | "
                f"{chapter_info.get('scene_location', '')}"
            )

        prompt = prompt_definitions.knowledge_filter_prompt.format(
            chapter_info=formatted_chapter_info,
            retrieved_texts="\n\n".join(formatted_texts) if formatted_texts else "（无检索结果）"
        )
        
        filtered_content = invoke_with_cleaning(llm_adapter, prompt)
        return filtered_content if filtered_content else "（知识内容过滤失败）"
        
    except Exception as e:
        logging.error(f"Error in knowledge filtering: {str(e)}")
        return "（内容过滤过程出错）"

def build_chapter_prompt(
    api_key: str,
    base_url: str,
    model_name: str,
    filepath: str,
    novel_number: int,
    word_number: int,
    temperature: float,
    user_guidance: str,
    characters_involved: str,
    key_items: str,
    scene_location: str,
    time_constraint: str,
    embedding_api_key: str,
    embedding_url: str,
    embedding_interface_format: str,
    embedding_model_name: str,
    embedding_retrieval_k: int = 2,
    interface_format: str = "openai",
    max_tokens: int = 2048,
    timeout: int = 600
) -> str:
    """
    构造当前章节的请求提示词（完整实现版）
    修改重点：
    1. 优化知识库检索流程
    2. 新增内容重复检测机制
    3. 集成提示词应用规则
    """
    # 读取基础文件
    arch_file = os.path.join(filepath, "Novel_architecture.txt")
    novel_architecture_text = read_file(arch_file)
    directory_file = os.path.join(filepath, "Novel_directory.txt")
    blueprint_text = read_file(directory_file)
    global_summary_file = os.path.join(filepath, "global_summary.txt")
    global_summary_text = read_file(global_summary_file)
    character_state_file = os.path.join(filepath, "character_state.txt")
    character_state_text = read_file(character_state_file)
    
    # 获取章节信息
    chapter_info = get_chapter_info_from_blueprint(blueprint_text, novel_number)
    chapter_title = chapter_info["chapter_title"]
    chapter_role = chapter_info["chapter_role"]
    chapter_purpose = chapter_info["chapter_purpose"]
    suspense_level = chapter_info["suspense_level"]
    foreshadowing = chapter_info["foreshadowing"]
    plot_twist_level = chapter_info["plot_twist_level"]
    chapter_summary = chapter_info["chapter_summary"]

    # 获取下一章节信息
    next_chapter_number = novel_number + 1
    next_chapter_info = get_chapter_info_from_blueprint(blueprint_text, next_chapter_number)
    next_chapter_title = next_chapter_info.get("chapter_title", "（未命名）")
    next_chapter_role = next_chapter_info.get("chapter_role", "过渡章节")
    next_chapter_purpose = next_chapter_info.get("chapter_purpose", "承上启下")
    next_chapter_suspense = next_chapter_info.get("suspense_level", "中等")
    next_chapter_foreshadow = next_chapter_info.get("foreshadowing", "无特殊伏笔")
    next_chapter_twist = next_chapter_info.get("plot_twist_level", "★☆☆☆☆")
    next_chapter_summary = next_chapter_info.get("chapter_summary", "衔接过渡内容")

    # 创建章节目录
    chapters_dir = os.path.join(filepath, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)

    # 第一章特殊处理
    if novel_number == 1:
        scene_plan = generate_chapter_scene_plan(
            interface_format=interface_format,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            novel_number=novel_number,
            chapter_title=chapter_title,
            novel_architecture=novel_architecture_text,
            global_summary="（首章，无前文摘要）",
            short_summary="（首章，无近章摘要）",
            previous_chapter_excerpt="（首章，无前章）",
            character_state=character_state_text or "（未提供角色状态）",
            chapter_role=chapter_role,
            chapter_purpose=chapter_purpose,
            suspense_level=suspense_level,
            foreshadowing=foreshadowing,
            plot_twist_level=plot_twist_level,
            chapter_summary=chapter_summary,
            characters_involved=characters_involved or "未指定",
            key_items=key_items or "未指定",
            scene_location=scene_location or "未指定",
            time_constraint=time_constraint or "未指定",
            user_guidance=user_guidance or "无特殊指导",
            next_chapter_summary=next_chapter_info.get("chapter_summary", "首章结尾为后续章节留下自然承接点。")
        )
        return prompt_definitions.first_chapter_draft_prompt.format(
            novel_number=novel_number,
            word_number=word_number,
            chapter_title=chapter_title,
            chapter_role=chapter_role,
            chapter_purpose=chapter_purpose,
            suspense_level=suspense_level,
            foreshadowing=foreshadowing,
            plot_twist_level=plot_twist_level,
            chapter_summary=chapter_summary,
            characters_involved=characters_involved,
            key_items=key_items,
            scene_location=scene_location,
            time_constraint=time_constraint,
            user_guidance=user_guidance,
            novel_setting=novel_architecture_text,
            scene_plan=scene_plan
        )

    # 获取前文内容和摘要
    recent_texts = get_last_n_chapters_text(chapters_dir, novel_number, n=3)
    
    try:
        logging.info("Attempting to generate summary")
        short_summary = summarize_recent_chapters(
            interface_format=interface_format,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            chapters_text_list=recent_texts,
            novel_number=novel_number,
            chapter_info=chapter_info,
            next_chapter_info=next_chapter_info,
            timeout=timeout
        )
        logging.info("Summary generated successfully")
    except Exception as e:
        logging.error(f"Error in summarize_recent_chapters: {str(e)}")
        short_summary = "（摘要生成失败）"

    # 获取前一章结尾
    previous_excerpt = ""
    for text in reversed(recent_texts):
        if text.strip():
            previous_excerpt = text[-800:] if len(text) > 800 else text
            break

    # 知识库检索和处理
    try:
        # 生成检索关键词
        llm_adapter = create_llm_adapter(
            interface_format=interface_format,
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout
        )
        
        search_prompt = prompt_definitions.knowledge_search_prompt.format(
            chapter_number=novel_number,
            chapter_title=chapter_title,
            characters_involved=characters_involved,
            key_items=key_items,
            scene_location=scene_location,
            chapter_role=chapter_role,
            chapter_purpose=chapter_purpose,
            foreshadowing=foreshadowing,
            short_summary=short_summary,
            user_guidance=user_guidance,
            time_constraint=time_constraint
        )
        
        search_response = invoke_with_cleaning(llm_adapter, search_prompt)
        keyword_groups = parse_search_keywords(search_response)

        # 执行向量检索
        all_contexts = []
        from embedding_adapters import create_embedding_adapter
        embedding_adapter = create_embedding_adapter(
            embedding_interface_format,
            embedding_api_key,
            embedding_url,
            embedding_model_name
        )
        
        store = load_vector_store(embedding_adapter, filepath)
        if store:
            collection_size = store._collection.count()
            actual_k = min(embedding_retrieval_k, max(1, collection_size))
            
            for group in keyword_groups:
                context = get_relevant_context_from_vector_store(
                    embedding_adapter=embedding_adapter,
                    query=group,
                    filepath=filepath,
                    k=actual_k
                )
                if context:
                    if any(kw in group.lower() for kw in ["技法", "手法", "模板"]):
                        all_contexts.append(f"[TECHNIQUE] {context}")
                    elif any(kw in group.lower() for kw in ["设定", "技术", "世界观"]):
                        all_contexts.append(f"[SETTING] {context}")
                    else:
                        all_contexts.append(f"[GENERAL] {context}")

        # 应用内容规则
        processed_contexts = apply_content_rules(all_contexts, novel_number)
        
        # 执行知识过滤
        chapter_info_for_filter = {
            "chapter_number": novel_number,
            "chapter_title": chapter_title,
            "chapter_role": chapter_role,
            "chapter_purpose": chapter_purpose,
            "characters_involved": characters_involved,
            "key_items": key_items,
            "scene_location": scene_location,
            "foreshadowing": foreshadowing,  # 修复拼写错误
            "suspense_level": suspense_level,
            "plot_twist_level": plot_twist_level,
            "chapter_summary": chapter_summary,
            "time_constraint": time_constraint
        }
        
        filtered_context = get_filtered_knowledge_context(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            interface_format=interface_format,
            embedding_adapter=embedding_adapter,
            filepath=filepath,
            chapter_info=chapter_info_for_filter,
            retrieved_texts=processed_contexts,
            max_tokens=max_tokens,
            timeout=timeout
        )
        
    except Exception as e:
        logging.error(f"知识处理流程异常：{str(e)}")
        filtered_context = "（知识库处理失败）"

    scene_plan = generate_chapter_scene_plan(
        interface_format=interface_format,
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        novel_number=novel_number,
        chapter_title=chapter_title,
        novel_architecture=novel_architecture_text,
        global_summary=global_summary_text or "（暂无全局摘要）",
        short_summary=short_summary,
        previous_chapter_excerpt=previous_excerpt or "（无可用前章结尾）",
        character_state=character_state_text or "（暂无角色状态）",
        chapter_role=chapter_role,
        chapter_purpose=chapter_purpose,
        suspense_level=suspense_level,
        foreshadowing=foreshadowing,
        plot_twist_level=plot_twist_level,
        chapter_summary=chapter_summary,
        characters_involved=characters_involved or "未指定",
        key_items=key_items or "未指定",
        scene_location=scene_location or "未指定",
        time_constraint=time_constraint or "未指定",
        user_guidance=user_guidance or "无特殊指导",
        next_chapter_summary=next_chapter_info.get("chapter_summary", "（本书没有下一章目录，不应额外展开后续章节）")
    )

    # 返回最终提示词
    return prompt_definitions.next_chapter_draft_prompt.format(
        user_guidance=user_guidance if user_guidance else "无特殊指导",
        global_summary=global_summary_text,
        previous_chapter_excerpt=previous_excerpt,
        character_state=character_state_text,
        short_summary=short_summary,
        scene_plan=scene_plan,
        novel_number=novel_number,
        chapter_title=chapter_title,
        chapter_role=chapter_role,
        chapter_purpose=chapter_purpose,
        suspense_level=suspense_level,
        foreshadowing=foreshadowing,
        plot_twist_level=plot_twist_level,
        chapter_summary=chapter_summary,
        word_number=word_number,
        characters_involved=characters_involved,
        key_items=key_items,
        scene_location=scene_location,
        time_constraint=time_constraint,
        next_chapter_number=next_chapter_number,
        next_chapter_title=next_chapter_title,
        next_chapter_role=next_chapter_role,
        next_chapter_purpose=next_chapter_purpose,
        next_chapter_suspense_level=next_chapter_suspense,
        next_chapter_foreshadowing=next_chapter_foreshadow,
        next_chapter_plot_twist_level=next_chapter_twist,
        next_chapter_summary=next_chapter_summary,
        filtered_context=filtered_context
    )

def generate_chapter_draft(
    api_key: str,
    base_url: str,
    model_name: str, 
    filepath: str,
    novel_number: int,
    word_number: int,
    temperature: float,
    user_guidance: str,
    characters_involved: str,
    key_items: str,
    scene_location: str,
    time_constraint: str,
    embedding_api_key: str,
    embedding_url: str,
    embedding_interface_format: str,
    embedding_model_name: str,
    embedding_retrieval_k: int = 2,
    interface_format: str = "openai",
    max_tokens: int = 2048,
    timeout: int = 600,
    custom_prompt_text: str = None
) -> str:
    """
    生成章节草稿，支持自定义提示词
    """
    if custom_prompt_text is None:
        prompt_text = build_chapter_prompt(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            filepath=filepath,
            novel_number=novel_number,
            word_number=word_number,
            temperature=temperature,
            user_guidance=user_guidance,
            characters_involved=characters_involved,
            key_items=key_items,
            scene_location=scene_location,
            time_constraint=time_constraint,
            embedding_api_key=embedding_api_key,
            embedding_url=embedding_url,
            embedding_interface_format=embedding_interface_format,
            embedding_model_name=embedding_model_name,
            embedding_retrieval_k=embedding_retrieval_k,
            interface_format=interface_format,
            max_tokens=max_tokens,
            timeout=timeout
        )
    else:
        prompt_text = custom_prompt_text

    chapters_dir = os.path.join(filepath, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)

    llm_adapter = create_llm_adapter(
        interface_format=interface_format,
        base_url=base_url,
        model_name=model_name,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout
    )

    chapter_content = invoke_with_cleaning(llm_adapter, prompt_text)
    if not chapter_content.strip():
        logging.warning("Generated chapter draft is empty.")
        return ""
    chapter_file = os.path.join(chapters_dir, f"chapter_{novel_number}.txt")
    clear_file_content(chapter_file)
    save_string_to_txt(chapter_content, chapter_file)
    logging.info(f"[Draft] Chapter {novel_number} generated as a draft.")
    return chapter_content
