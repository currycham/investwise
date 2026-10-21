"""投资画像：用户选的标签 + 自己写的理念 + 历史行为（持仓、投资逻辑、决策、判断、对话），由 AI 总结成画像。

画像有两个用途：帮用户看清自己（尤其是"说的"和"做的"不一致的地方）；决定资讯的解读角度、数据页默认显示的指标和关注点。
"""
import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import llm
from .auth import current_user
from .catalog import ALL_TAGS, CONCERNS, DEFAULT_CONCERNS, DEFAULT_METRICS, METRICS, catalog_out
from .db import get_conn, thesis_text
from .prompts import PROFILE_SYSTEM

router = APIRouter(prefix="/api")
MARKET = {"A": "A股", "HK": "港股", "US": "美股"}


# ---------- 偏好（数据页指标、关注点、仓位填写方式） ----------

def prefs(user: dict) -> dict:
    saved = json.loads(user.get("data_prefs_json") or "{}")
    metrics = [k for k in saved.get("metrics") or DEFAULT_METRICS if k in METRICS]
    saved_concerns = saved.get("concerns")
    concerns = {}
    for k, (_, _, threshold, _, _) in CONCERNS.items():
        s = (saved_concerns or {}).get(k) or {}
        concerns[k] = {"on": bool(s.get("on")) if saved_concerns else k in DEFAULT_CONCERNS,
                       "threshold": s.get("threshold", threshold)}
    return {"metrics": metrics, "concerns": concerns, "position_mode": saved.get("position_mode") or "shares",
            "custom": bool(saved.get("custom"))}


def save_prefs(user_id: int, p: dict) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET data_prefs_json = ? WHERE id = ?", (json.dumps(p, ensure_ascii=False), user_id))


def profile_of(user: dict) -> dict | None:
    return json.loads(user["profile_json"]) if user.get("profile_json") else None


def brief(user: dict) -> str:
    """给其他提示词用的简短画像；没有画像时用标签凑一句；都没有返回空串。"""
    p = profile_of(user)
    tags = json.loads(user.get("profile_tags_json") or "[]")
    if p:
        styles = "、".join(s.get("name", "") for s in p.get("styles") or [])
        return (f"{p.get('headline', '')}（风格：{styles}）\n资讯侧重：{p.get('news_focus', '')}").strip()
    return f"用户自选标签：{'、'.join(tags)}" if tags else ""


# ---------- 接口 ----------

@router.get("/profile")
def get_profile(user: dict = Depends(current_user)):
    with get_conn() as conn:
        stats = _history_stats(conn, user["id"])
    return {"catalog": catalog_out(), "tags": json.loads(user["profile_tags_json"] or "[]"),
            "statement": user["profile_statement"], "profile": profile_of(user), "profile_at": user["profile_at"],
            "prefs": prefs(user), "history": stats}


class TagsIn(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=40)
    statement: str = Field(default="", max_length=2000)


@router.put("/profile/tags")
def save_tags(body: TagsIn, user: dict = Depends(current_user)):
    tags = [t for t in dict.fromkeys(body.tags) if t in ALL_TAGS]
    with get_conn() as conn:
        conn.execute("UPDATE users SET profile_tags_json = ?, profile_statement = ? WHERE id = ?",
                     (json.dumps(tags, ensure_ascii=False), body.statement.strip(), user["id"]))
    return {"ok": True}


class PrefsIn(BaseModel):
    metrics: list[str] = Field(default_factory=list, max_length=40)
    concerns: dict[str, dict] = Field(default_factory=dict)
    position_mode: str = Field(default="shares", pattern="^(shares|weight)$")


@router.put("/profile/prefs")
def update_prefs(body: PrefsIn, user: dict = Depends(current_user)):
    concerns = {}
    for k, v in body.concerns.items():
        if k in CONCERNS:
            try:
                threshold = float(v.get("threshold", CONCERNS[k][2]))
            except (TypeError, ValueError):
                threshold = CONCERNS[k][2]
            concerns[k] = {"on": bool(v.get("on")), "threshold": threshold}
    save_prefs(user["id"], {"metrics": [k for k in dict.fromkeys(body.metrics) if k in METRICS],
                            "concerns": concerns, "position_mode": body.position_mode, "custom": True})
    return {"ok": True}


@router.post("/profile/analyze")
def analyze(user: dict = Depends(current_user)):
    tags = json.loads(user["profile_tags_json"] or "[]")
    with get_conn() as conn:
        material, stats = _material(conn, user), _history_stats(conn, user["id"])
    if not tags and not user["profile_statement"].strip() and not stats["companies"]:
        raise HTTPException(400, "先选几个标签、写几句你的投资理念，或者加入几家持仓，AI 才有材料可分析")
    try:
        out = llm.chat_json([{"role": "system", "content": PROFILE_SYSTEM}, {"role": "user", "content": material}],
                            temperature=0.4, max_tokens=2500, task="profile", user_id=user["id"])
    except llm.BudgetError as e:
        raise HTTPException(429, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    lst = lambda k, n: [str(x) for x in out.get(k) or [] if x][:n]  # noqa: E731
    profile = {
        "headline": str(out.get("headline") or ""),
        "styles": [{"name": str(s.get("name")), "weight": max(0.0, min(1.0, float(s.get("weight") or 0)))}
                   for s in out.get("styles") or [] if isinstance(s, dict) and s.get("name")][:5],
        "philosophy": lst("philosophy", 5), "evidence": lst("evidence", 5), "gaps": lst("gaps", 4),
        "strengths": lst("strengths", 3), "pitfalls": lst("pitfalls", 3), "questions": lst("questions", 3),
        "news_focus": str(out.get("news_focus") or ""),
        "metrics": [k for k in out.get("metrics") or [] if k in METRICS][:14],
        "concerns": [k for k in out.get("concerns") or [] if k in CONCERNS][:12],
        "based_on": stats,
    }
    with get_conn() as conn:
        conn.execute("UPDATE users SET profile_json = ?, profile_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (json.dumps(profile, ensure_ascii=False), user["id"]))
    p = prefs(user)
    if not p["custom"]:  # 用户没自己调过：数据页按画像推荐的指标和关注点
        apply_recommendation(user["id"], p, profile)
    return {"profile": profile}


def apply_recommendation(user_id: int, p: dict, profile: dict) -> None:
    metrics = profile["metrics"] or DEFAULT_METRICS
    if "weight" not in metrics:
        metrics = ["weight", *metrics]
    if "industry" not in metrics:
        metrics = ["industry", *metrics]
    rec = set(profile["concerns"] or DEFAULT_CONCERNS) | {"thesis_weakened"}
    concerns = {k: {"on": k in rec, "threshold": v["threshold"]} for k, v in p["concerns"].items()}
    save_prefs(user_id, {"metrics": metrics, "concerns": concerns, "position_mode": p["position_mode"],
                         "custom": False})


@router.post("/profile/apply")
def apply(user: dict = Depends(current_user)):
    """把数据页恢复成画像推荐的指标和关注点。"""
    profile = profile_of(user)
    if not profile:
        raise HTTPException(400, "还没有画像")
    apply_recommendation(user["id"], prefs(user), profile)
    return {"ok": True}


# ---------- 给模型的材料 ----------

def _history_stats(conn, user_id: int) -> dict:
    one = lambda sql: conn.execute(sql, (user_id,)).fetchone()[0]  # noqa: E731
    return {
        "companies": one("SELECT COUNT(*) FROM companies WHERE user_id = ?"),
        "theses": one("SELECT COUNT(*) FROM companies WHERE user_id = ? AND (thesis != '' OR assumptions != '')"),
        "decisions": one("SELECT COUNT(*) FROM decisions WHERE user_id = ?"),
        "judgments": one("SELECT COUNT(*) FROM item_judgments WHERE user_id = ?"),
        "closed": one("SELECT COUNT(*) FROM closed_trades WHERE user_id = ?"),
        "chats": one("SELECT COUNT(*) FROM chat_messages m JOIN companies c ON c.id = m.company_id"
                     " WHERE c.user_id = ? AND m.role = 'user'"),
    }


def _material(conn, user: dict) -> str:
    uid = user["id"]
    tags = json.loads(user["profile_tags_json"] or "[]")
    companies = [dict(r) for r in conn.execute(
        "SELECT c.*, s.industry FROM companies c LEFT JOIN stock_data s ON s.market = c.market AND s.ticker = c.ticker"
        " WHERE c.user_id = ? ORDER BY c.status, c.manual_weight DESC", (uid,))]
    status = {"holding": "持仓中", "watching": "观察中", "closed": "已清仓"}
    lines = []
    for c in companies:
        t = thesis_text(c)
        weight = f"，权重 {c['manual_weight']:g}%" if c["manual_weight"] else ""
        lines.append(f"- {c['name']}（{MARKET[c['market']]}，{c['industry'] or '行业未知'}，{status[c['status']]}{weight}）"
                     + (f"\n  投资逻辑：{t[:300]}" if t else ""))
    industries = Counter(c["industry"] for c in companies if c["industry"] and c["status"] != "closed")
    trades = conn.execute(
        "SELECT t.*, c.name FROM closed_trades t JOIN companies c ON c.id = t.company_id WHERE t.user_id = ?"
        " ORDER BY t.closed_at DESC LIMIT 15", (uid,)).fetchall()
    decisions = conn.execute(
        "SELECT d.*, c.name FROM decisions d JOIN companies c ON c.id = d.company_id WHERE d.user_id = ?"
        " ORDER BY d.decided_on DESC LIMIT 30", (uid,)).fetchall()
    judg = conn.execute(
        "SELECT j.verdict, ri.direction FROM item_judgments j JOIN report_items ri ON ri.id = j.item_id"
        " WHERE j.user_id = ?", (uid,)).fetchall()
    chats = conn.execute(
        "SELECT c.name, m.content FROM chat_messages m JOIN companies c ON c.id = m.company_id"
        " WHERE c.user_id = ? AND m.role = 'user' ORDER BY m.id DESC LIMIT 25", (uid,)).fetchall()

    parts = [f"## 用户自选的标签\n{'、'.join(tags) or '（没选）'}",
             f"## 用户用自己的话写的投资理念\n{user['profile_statement'].strip() or '（没写）'}",
             f"## 持仓与关注（共 {len(companies)} 家）\n" + ("\n".join(lines) or "（还没有）")]
    if industries:
        parts.append("## 行业分布\n" + "、".join(f"{k} {v} 家" for k, v in industries.most_common(8)))
    if trades:
        parts.append("## 清仓记录\n" + "\n".join(
            f"- {t['closed_at']} {t['name']} {t['outcome']}"
            + (f"，收益率 {t['pnl_pct']:+.1f}%" if t["pnl_pct"] is not None else "") + f"：{t['note'][:150]}"
            for t in trades))
    if decisions:
        parts.append("## 决策日志（最近 30 条）\n" + "\n".join(
            f"- {d['decided_on']} {d['name']} {d['action']}（把握 {d['confidence']}/5"
            f"{'，情绪：' + d['emotion'] if d['emotion'] else ''}）：{d['reason'][:200]}"
            + (f"\n  事后复盘：{d['review_note'][:150]}" if d["review_note"] else "") for d in decisions))
    if judg:
        agree = sum(1 for j in judg if j["verdict"] == j["direction"])
        mix = Counter(f"你判{j['verdict']}/AI判{j['direction']}" for j in judg if j["verdict"] != j["direction"])
        parts.append(f"## 「先判断再看 AI」的记录\n共 {len(judg)} 条，与 AI 一致 {agree} 条。不一致的分布："
                     + ("、".join(f"{k} {v} 次" for k, v in mix.most_common()) or "无"))
    if chats:
        parts.append("## 用户在提问助手里说过的话（最近 25 条）\n" + "\n".join(
            f"- 关于{c['name']}：{c['content'][:200]}" for c in chats))
    parts.append("## 可选的数据指标 key\n" + "、".join(f"{k}（{v[0]}）" for k, v in METRICS.items()))
    parts.append("## 可选的关注点规则 key\n" + "、".join(f"{k}（{v[0]}）" for k, v in CONCERNS.items()))
    return "\n\n".join(parts)
