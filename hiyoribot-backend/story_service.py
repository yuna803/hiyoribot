"""原作跳转图、会话进度和两个检索入口共用的剧情边界。"""

from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import storage


class StoryProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    route: Literal["common", "hiyori"] = "common"
    script_name: str | None = Field(default=None, max_length=100)
    entry_no: int = Field(default=0, ge=0)
    completed_scripts: list[str] = Field(default_factory=list, max_length=87)
    relationship: str = Field(default="兄妹", min_length=1, max_length=100)
    scene: str = Field(default="", max_length=500)


def graph_info(chapters: list[dict], edges: list[dict]) -> dict:
    """求实际跳转图中的必经前置；分支兄弟与循环不能靠文件名排序。"""
    nodes = {row["script_name"] for row in chapters}
    successors = {name: set() for name in nodes}
    predecessors = {name: set() for name in nodes}
    for edge in edges:
        source, target = edge["source_script"], edge["target_script"]
        if source in nodes and target in nodes and source != target:
            successors[source].add(target)
            predecessors[target].add(source)

    def walk(start: str, links: dict) -> set[str]:
        found, pending = set(), [start]
        while pending:
            name = pending.pop()
            if name in found:
                continue
            found.add(name)
            pending.extend(links.get(name, ()))
        return found

    root = "共通-01.ast"
    reachable = walk(root, successors) & nodes
    # 支配关系：所有能到达当前章节的路径都经过该前置章节。
    dominators = {name: ({root} if name == root else set(reachable)) for name in reachable}
    changed = True
    while changed:
        changed = False
        for name in sorted(reachable - {root}):
            parents = predecessors[name] & reachable
            value = {name} | set.intersection(*(dominators[p] for p in parents))
            if value != dominators[name]:
                dominators[name], changed = value, True
    distances, queue = {root: 0}, deque([root])
    while queue:
        for target in sorted(successors.get(queue.popleft(), ())):
            if target not in distances:
                distances[target] = min(distances[p] for p in predecessors[target] if p in distances) + 1
                queue.append(target)
    return {"predecessors": predecessors, "successors": successors,
            "ancestors": {name: walk(name, predecessors) - {name} for name in nodes},
            "dominators": dominators, "distance": distances}


def validate_progress(progress: StoryProgress, chapters: list[dict], edges: list[dict]) -> dict:
    rows = {row["script_name"]: row for row in chapters}
    name = progress.script_name
    if name is None:
        if progress.completed_scripts or progress.entry_no:
            raise ValueError("请先选择当前章节")
        return progress.model_dump()
    if name not in rows:
        raise ValueError("当前章节未导入，不能用文件名推测顺序")
    if progress.route == "common" and rows[name]["scope"] != "common":
        raise ValueError("共通线进度不能选择妃爱线章节")
    if progress.entry_no > rows[name]["max_entry"]:
        raise ValueError("台词序号超过当前章节范围")
    info = graph_info(chapters, edges)
    linked = name in info["dominators"]
    seen = set(progress.completed_scripts)
    if len(seen) != len(progress.completed_scripts) or name in seen:
        raise ValueError("已发生分支不能重复或包含当前章节")
    for completed in seen:
        if completed not in rows or (progress.route == "common" and rows[completed]["scope"] != "common"):
            raise ValueError("已发生分支不属于当前路线")
        if linked and completed not in info["ancestors"][name]:
            raise ValueError("不能将当前章节之后或其他结局的章节标为已发生")
    for first in seen:
        for second in seen - {first}:
            if first not in info["ancestors"][second] and second not in info["ancestors"][first]:
                raise ValueError("这些分支没有同一路径的顺序证据，请勿同时标为已发生")
    return progress.model_dump()


def timeline() -> dict:
    chapters, edges = storage.story_graph()
    info = graph_info(chapters, edges)
    items = []
    for row in sorted(chapters, key=lambda r: (info["distance"].get(r["script_name"], 10000), r["script_name"])):
        name = row["script_name"]
        mandatory = info["dominators"].get(name, set()) - {name}
        items.append({**row, "linked": name in info["dominators"],
                      "mandatory_predecessors": sorted(mandatory),
                      "optional_predecessors": sorted(info["ancestors"][name] - mandatory),
                      "successors": sorted(info["successors"][name])})
    return {"chapters": items, "edges": edges,
            "note": "顺序来自脚本跳转；条件分支不自动算作已发生。独立场景的前置需手动确认。"}


def bounds_for_progress(progress: dict | None, chapters: list[dict], edges: list[dict]) -> dict[str, int]:
    state = StoryProgress.model_validate(progress or {})
    validate_progress(state, chapters, edges)
    if state.script_name is None:
        return {}
    info = graph_info(chapters, edges)
    rows = {row["script_name"]: row for row in chapters}
    current = state.script_name
    mandatory = info["dominators"].get(current, set()) - {current}
    ancestors = info["ancestors"][current] | {current}
    bounds = {}
    for name in mandatory:
        # 中途调用另一个脚本，不代表调用者余下的台词已经发生。
        exits = [edge["source_entry"] for edge in edges
                 if edge["source_script"] == name and edge["target_script"] in ancestors]
        bounds[name] = min([rows[name]["max_entry"], *exits])
    for name in state.completed_scripts:
        bounds[name] = rows[name]["max_entry"]
    bounds[current] = state.entry_no
    # 内部 label 分支尚未执行、也未人工核对；不能把多个选择的文本都当成回忆。
    for name in list(bounds):
        uncertain = rows[name].get("uncertain_from")
        if uncertain is not None:
            bounds[name] = min(bounds[name], uncertain - 1)
        if bounds[name] < 0:
            del bounds[name]
    return bounds


def recall(embedding: list[float], character_name: str, progress: dict | None,
           *, notes_limit: int = 4, dialogue_limit: int = 2) -> tuple[list[dict], list[dict]]:
    if character_name != "和泉妃爱":
        notes = storage.recall_role_knowledge(embedding, character_name, limit=notes_limit)
        return [row for row in notes if float(row["similarity"]) >= 0.5], []
    chapters, edges = storage.story_graph()
    bounds = bounds_for_progress(progress, chapters, edges)
    if not bounds:
        return [], []
    # 旧摘要的 source_key 是文件行号，不能冒充台词序号；仅放行整章已完成的摘要。
    complete = [row["script_name"] for row in chapters
                if bounds.get(row["script_name"], -1) >= row["max_entry"]]
    notes = storage.recall_role_knowledge(embedding, character_name, limit=notes_limit,
                                          completed_scripts=complete)
    hits = storage.recall_game_dialogue(embedding, limit=dialogue_limit * 3, bounds=bounds)
    hits = [row for row in hits if float(row["similarity"]) >= 0.4][:dialogue_limit]
    expanded = storage.expand_game_dialogue(hits, bounds)
    return [row for row in notes if float(row["similarity"]) >= 0.5], expanded


def prompt(progress: dict | None) -> str:
    state = StoryProgress.model_validate(progress or {})
    chapter = f"{state.script_name}，截至台词 #{state.entry_no}" if state.script_name else "尚未设置，不能声称原作事件已经发生"
    return ("当前会话的角色扮演进度（由用户设定，不自行推进）：\n"
            f"路线：{'共通线' if state.route == 'common' else '妃爱线'}；当前章节：{chapter}。\n"
            f"对话者始终是和泉智宏，也就是哥哥；当前关系：{state.relationship}。\n"
            f"当前场景：{state.scene or '未指定，不擅自补充具体日期或地点'}。\n"
            "原作参考片段均受进度限制；文件序号不是日历日期，回忆或倒叙也不等于当前发生。"
            "未确认分支和后续剧情不能当作已经共同经历；旧助手回复不能证明原作事件发生。"
            "关系变化以本会话设定为准，不因召回后期台词就自动认定已经交往。")
