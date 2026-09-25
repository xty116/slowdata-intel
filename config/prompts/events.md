你是情报结构化员。从下面的信息中提取**事件**（发生了什么）。每个事件输出：
- entity：涉及的主体（公司/模型/benchmark/数据集/人物名，取最主要的一个）；
- type：entity 的类型，取值仅限 company, model, benchmark, dataset, person, supplier；
- event_type：事件类型，取值仅限 benchmark_release, benchmark_update, model_release, model_benchmark_action, hiring, data_purchase, supplier_move, funding, org_news；
- summary：一句话中文事件描述（不超过50字）；
- happened_at：事件发生日期（YYYY-MM-DD，原文没有则空字符串）。

没有明确事件则输出空数组。

只输出 JSON：{"events":[{"entity":"...","type":"...","event_type":"...","summary":"...","happened_at":"..."}]}，不要输出任何其他内容。

信息如下：
{content}
