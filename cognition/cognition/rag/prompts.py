"""RAG 各节点提示词（纯常量）。ANSWER 强约束防幻觉。"""

from __future__ import annotations

ROUTE_PROMPT = (
    "判断下面的问题是否需要查询知识库才能准确回答。\n"
    "常识、问候、寒暄、闲聊等无需外部知识的，回答 NO；需要事实/文档依据的，回答 YES。\n"
    "问题：{query}\n"
    "只回答 YES 或 NO。"
)

EXPAND_PROMPT = (
    "把下面的问题拆解成 1-{max} 个便于向量检索的子问题（覆盖不同侧面），"
    "每行一个，不要编号、不要解释：\n{query}"
)

REFLECT_PROMPT = (
    "已检索到以下证据：\n{evidence}\n\n"
    "针对问题「{query}」，这些证据是否足够、准确地作答？\n"
    '只输出 JSON：{{"is_answer": true/false, "rewrite_query": "若不足，给出改写后的检索问题；否则空串"}}'
)

ANSWER_PROMPT = (
    "根据下列证据回答问题。要求：\n"
    "1) 只依据证据作答，严禁编造证据未提及的信息；证据不足就说明无法确定。\n"
    "2) 在相关句子后用〔n〕标注所引用的证据编号。\n"
    "3) 去重、简洁。\n\n"
    "证据：\n{context}\n\n问题：{query}\n\n回答："
)

DIRECT_PROMPT = "直接、简洁地回答下面的问题：\n{query}"
