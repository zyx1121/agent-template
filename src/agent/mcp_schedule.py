"""Standalone stdio MCP server exposing schedule CRUD to claude — the ONLY sanctioned way a
turn creates a persistent, guaranteed-to-fire reminder or scheduled task. `claude.py` spawns
one fresh `python -m agent.mcp_schedule` subprocess per turn (wired in via the runtime
mcp-config it writes to `run/mcp-runtime-<chat_id>.json`), passing `AGENT_HOME` (where
schedules.json lives) and `AGENT_CHAT_ID` (which chat schedule_add should target) as env vars
— never argv or cwd, since claude controls the exact command/args/env from that config file.

This module is a thin protocol adapter. The actual schedule storage lives in
schedule_store.py (shared with the bot's own long-running process), and the actual firing
happens in handlers.py's JobQueue tick — nothing in this process ever executes a schedule,
it only reads/writes the JSON file the tick polls.

Run directly for a smoke test: `AGENT_HOME=. AGENT_CHAT_ID=123 python -m agent.mcp_schedule`
(stdio JSON-RPC on stdin/stdout, per the MCP spec).
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from agent.cron import validate_cron
from agent.schedule_store import add_schedule, edit_schedule, list_schedules, remove_schedule

mcp = FastMCP("schedule")


def _schedules_file() -> Path:
    home = Path(os.environ.get("AGENT_HOME") or Path.cwd())
    return home / "run" / "schedules.json"


def _chat_id() -> int:
    raw = os.environ.get("AGENT_CHAT_ID")
    if not raw:
        raise RuntimeError(
            "AGENT_CHAT_ID not set — this server must be launched per-turn by claude.py, "
            "not run standalone against a live chat"
        )
    return int(raw)


@mcp.tool()
def schedule_add(cron: str, prompt: str, note: str = "", once: bool = False) -> str:
    """建立一個持久排程,到點會在「這個」聊天視窗自動跑一輪 claude、並把結果傳回這裡 —— 這是
    排程/提醒/定時任務唯一可靠的機制。排程存在 bot 的長駐 process 裡,不受這次對話結束影響。

    Args:
        cron: 標準 5 欄 cron 表達式(分 時 日 月 週),例如 "0 8 * * *"(每天 8:00)、
            "*/30 * * * *"(每 30 分鐘)、"0 9 * * 1-5"(平日早上 9 點)。週欄 0 和 7 都代表週日。
        prompt: 到點後要交給 claude 執行的任務內容 —— 用完整的自然語言描述,執行時不會有
            這次對話的上下文,寫得越自包含越好。
        note: 給人看的簡短備註(schedule_list 會顯示),不影響執行邏輯。
        once: True 表示只執行一次,fire 後自動從排程刪除;False(預設)依 cron 重複執行。

    Returns:
        成功則回排程 id + 摘要;cron 格式錯誤則回清楚的錯誤訊息(不會建立)。
    """
    err = validate_cron(cron)
    if err:
        return f"❌ cron 格式錯誤:{err}"
    sched = add_schedule(
        _schedules_file(), cron=cron, prompt=prompt, chat_id=_chat_id(), note=note, once=once
    )
    return f"✅ 已建立排程 id={sched['id']} cron={cron} → {prompt[:60]}"


@mcp.tool()
def schedule_list() -> str:
    """列出所有排程,包含其他聊天視窗建立的(單一 host、單一使用者部署,不做 chat 間隔離)。
    改 (schedule_edit) 或刪 (schedule_remove) 一個排程之前,先用這個查它的 id。

    Returns:
        每個排程一行摘要:id、是否啟用、是否單次、cron、所屬 chat_id、note、prompt 開頭。
        沒有任何排程時明確說明,不回傳空字串。
    """
    schedules = list_schedules(_schedules_file())
    if not schedules:
        return "目前沒有任何排程。"
    lines = []
    for s in schedules:
        flag = "" if s.get("enabled", True) else "(已停用)"
        once_flag = " [單次]" if s.get("once") else ""
        note = f" — {s['note']}" if s.get("note") else ""
        lines.append(
            f"{s['id']}{flag}{once_flag} · {s['cron']} · chat {s['chat_id']}{note}\n"
            f"  {s['prompt'][:80]}"
        )
    return "\n".join(lines)


@mcp.tool()
def schedule_edit(
    id: str,
    cron: str | None = None,
    prompt: str | None = None,
    note: str | None = None,
    enabled: bool | None = None,
    once: bool | None = None,
) -> str:
    """修改一個既有排程,只傳要改的欄位,其他留空(None)不動。常見用法:enabled=False 暫停
    (不用刪掉重建)、改 cron 調時間、改 prompt 調任務內容。id 用 schedule_list() 查。

    Returns:
        更新後的排程摘要;id 不存在或 cron 格式錯誤則回清楚的錯誤訊息(不會套用)。
    """
    if cron is not None:
        err = validate_cron(cron)
        if err:
            return f"❌ cron 格式錯誤:{err}"
    sched = edit_schedule(
        _schedules_file(), id, cron=cron, prompt=prompt, note=note, enabled=enabled, once=once
    )
    if sched is None:
        return f"❌ 找不到排程 id={id}"
    return f"✅ 排程 id={sched['id']} 已更新 cron={sched['cron']} → {sched['prompt'][:60]}"


@mcp.tool()
def schedule_remove(id: str) -> str:
    """刪除一個排程,立即生效、不會再觸發。id 用 schedule_list() 查。"""
    ok = remove_schedule(_schedules_file(), id)
    return f"✅ 已刪除排程 {id}" if ok else f"❌ 找不到排程 id={id}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
