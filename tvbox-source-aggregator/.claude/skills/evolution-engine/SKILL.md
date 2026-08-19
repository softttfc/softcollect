---
name: evolution-engine
description: 当 session 初始化时自动触发，或用户手动触发。由 evolution-runner sub-agent 调用，扫描 feedback 积累并生成进化建议。
---

[任务]
    扫描 .claude/feedback/ 中的积累，识别三类进化信号：
    1. 规则毕业：feedback 重复 3+ 次 → 提议升级为正式规则
    2. Skill 优化：某 Skill 来源的 feedback 评分持续偏低 → 提议调整 Skill
    3. 新 Skill 提议：某操作模式反复出现但无 Skill 覆盖 → 提议创建新 Skill

    有信号 → 生成提议返回给主 Agent，由用户确认后执行。
    无信号 → 返回"无进化建议"。

[扫描流程]

    第一步：扫描毕业候选
        读取 .claude/feedback/FEEDBACK-INDEX.md 定位所有 feedback 文件
        读取每个文件的 frontmatter
        筛选：occurrences >= 3 且 graduated == false 且 skipped != true
        确定毕业目标：
        - source_skill 明确 → 毕业到对应 SKILL.md
        - 涉及多个 Skill 或全局性 → 毕业到 CLAUDE.md [总体规则]

    第二步：检查 Skill 优化信号
        扫描 feedback/ 中的 scores 字段，按 source_skill 分组
        触发条件（满足任一）：
        - 某 Skill 连续 3 次同一维度 <= 2 分
        - 某 Skill 某维度最近 5 次平均 <= 3 分
        - 某 Skill 来源的 feedback occurrences 合计 >= 5

    第三步：检查新 Skill 信号
        筛选：occurrences >= 5 且不属于任何已有 Skill 的覆盖范围
        → 标记为"新 Skill 候选"

    第四步：生成提议
        有信号 → 生成结构化提议（见 [提议格式]）
        无信号 → 返回"无进化建议"

[提议格式]
    "**进化建议**（共 N 条）

     **规则毕业**（X 条）
     1. [feedback 标题]：出现 [N] 次（来源：[source_skill]）
        建议写入：[目标文件] 的 [目标位置]
        内容摘要：[一句话]
        -- 确认 / 跳过

     **Skill 优化**（X 条）
     1. [Skill 名称]：累计 [N] 条相关 feedback
        优化建议：[具体建议]
        -- 确认 / 跳过

     **新 Skill 提议**（X 条）
     1. [操作模式描述]：出现 [N] 次
        -- 确认创建 / 跳过"

[确认后执行]
    用户逐条确认或跳过：
    - 规则毕业 → 将 feedback 内容写入目标 SKILL.md 或 CLAUDE.md，标记 graduated: true
    - Skill 优化 → 修改对应 SKILL.md
    - 新 Skill → 提议创建新 Skill（如有 skill-builder 则调用）
    - 跳过 → 标记 skipped: true，不再重复提议

[返回格式]
    返回给主 Agent：
    - 有提议："有 N 条进化建议待处理" + 完整提议内容
    - 无提议："无进化建议"
