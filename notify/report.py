"""The daily Telegram report — in French, for the operator.

What happened over the last 24 hours (fills, the bot's actions), where the
account stands (equity, the month's PnL, the 6% Rule), what is open (positions
with their Elder verdict and stop, the pending entry) and what the traded horizon
says now (its best setup). Telegram HTML; every value from the exchange or the
snapshot is escaped.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import Config
from execution.account import AccountView, realized_pnl
from execution.state import ExecutionState

VERDICT_FR = {"hold": "CONSERVER", "take_profits": "PRENDRE DES PROFITS", "exit": "SORTIR"}
SIDE_FR = {"long": "long", "short": "short"}
MODE_FR = {"dry_run": "DRY-RUN (aucun ordre envoyé)", "testnet": "TESTNET", "mainnet": "MAINNET"}


def _e(value: Any) -> str:
    return html.escape(str(value))


def _usd(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+,.2f} $" if signed else f"{x:,.2f} $"


def _num(x: float | None) -> str:
    return f"{x:,.6g}" if x is not None else "—"


def _local(ts: datetime, tz: ZoneInfo) -> str:
    return ts.astimezone(tz).strftime("%d/%m %H:%M")


def _fill_line(fill: Mapping[str, Any], tz: ZoneInfo) -> str:
    when = datetime.fromtimestamp(int(fill.get("time", 0)) / 1000, tz=UTC)
    pnl = float(fill.get("closedPnl", 0.0))
    pnl_part = f" · PnL clos {_usd(pnl, signed=True)}" if pnl else ""
    return (
        f"• {_local(when, tz)} <b>{_e(fill.get('coin', '?'))}</b> {_e(fill.get('dir', ''))} "
        f"{_e(fill.get('sz', '?'))} @ {_e(fill.get('px', '?'))} · frais "
        f"{_usd(float(fill.get('fee', 0.0)))}{pnl_part}"
    )


def build_report(
    cfg: Config,
    *,
    now: datetime,
    since: datetime,
    state: ExecutionState,
    account: AccountView,
    fills: Sequence[Mapping[str, Any]] | None,
    month_fills: Sequence[Mapping[str, Any]] | None,
    snapshot: Mapping[str, Any],
    halted: bool = False,
) -> str:
    tz = ZoneInfo(cfg.telegram.timezone)
    ecfg = cfg.execution
    mode = MODE_FR.get(ecfg.mode, ecfg.mode) if ecfg.enabled else "exécution désactivée"
    lines = [
        f"📊 <b>NTFAL — rapport du {now.astimezone(tz).strftime('%d/%m/%Y %H:%M')}</b>",
        f"Mode : <b>{_e(mode)}</b> · horizon tradé : <b>{_e(ecfg.horizon)}</b>",
    ]
    if halted:
        lines.append(f"⏸ <b>Pause</b> : le fichier {_e(ecfg.halt_file)} existe, aucune entrée.")

    # --- account ---------------------------------------------------------------
    lines.append("")
    lines.append("<b>Compte</b>")
    month_start_equity = (state.month or {}).get("equity")
    if account.equity is not None:
        change = (
            f" ({account.equity / month_start_equity - 1:+.2%} depuis le début du mois, "
            f"{_usd(month_start_equity)})"
            if month_start_equity
            else ""
        )
        lines.append(f"• Équité : <b>{_usd(account.equity)}</b>{change}")
    else:
        unread = ", ".join(f"'{d}'" if d else "natif" for d in sorted(account.failed_dexes))
        lines.append(f"• Équité : illisible ({_e(unread) or 'pas d’adresse configurée'})")
    if fills is not None:
        lines.append(
            f"• PnL réalisé 24 h (net de frais) : <b>{_usd(realized_pnl(fills), True)}</b>"
        )
    if month_fills is not None:
        lines.append(f"• PnL réalisé du mois : {_usd(realized_pnl(month_fills), True)}")
    guard = snapshot.get("guard") or {}
    if guard:
        status = "⛔ <b>BLOQUÉE</b> — aucune entrée ce mois-ci" if guard.get("blocked") else "OK"
        lines.append(
            f"• Règle des 6 % : {_usd(guard.get('total_at_risk'))} engagés sur "
            f"{_usd(guard.get('limit'))} — {status}"
        )

    # --- fills -------------------------------------------------------------------
    lines.append("")
    lines.append("<b>Exécutions (24 h)</b>")
    if fills is None:
        lines.append("• illisibles")
    elif not fills:
        lines.append("• aucune")
    else:
        lines.extend(_fill_line(f, tz) for f in fills)

    # --- the bot's actions ----------------------------------------------------------
    lines.append("")
    lines.append("<b>Actions du bot (24 h)</b>")
    events = [e for e in state.events_since(since) if e["kind"] != "note"]
    notes = [e for e in state.events_since(since) if e["kind"] == "note"]
    if not events:
        lines.append("• aucune")
    for e in events:
        icon = {"error": "🚨 ", "alert": "🔔 "}.get(e["level"], "")
        when = _local(datetime.fromisoformat(e["time"]), tz)
        lines.append(f"• {when} {icon}{_e(e['message'])}")
    if notes:
        # A note repeats each cycle; the latest one per asset says it all.
        latest: dict[Any, dict[str, Any]] = {}
        for n in notes:
            latest[n.get("asset")] = n
        lines.append("<i>Non fait, et pourquoi :</i>")
        lines.extend(f"• {_e(n['message'])}" for n in latest.values())

    # --- open positions ---------------------------------------------------------------
    lines.append("")
    lines.append("<b>Positions ouvertes</b>")
    verdicts = {p["asset"]: p for p in snapshot.get("positions", [])}
    if not account.positions:
        lines.append("• aucune")
    for pos in account.positions:
        owned = state.positions.get(pos.asset)
        v = verdicts.get(pos.asset) or {}
        stop = owned.stop if owned is not None else v.get("suggested_stop")
        who = "géré par le bot" if owned is not None else "manuel — non géré"
        pnl = pos.unrealized_pnl
        verdict = VERDICT_FR.get(v.get("verdict", ""), "—")
        funding = f" · funding payé {_usd(pos.cum_funding)}" if pos.cum_funding else ""
        lines.append(
            f"• <b>{_e(pos.asset)}</b> {SIDE_FR.get(pos.side, pos.side)} {pos.size:g} @ "
            f"{_num(pos.entry)} · mark {_num(pos.mark_price)} · PnL {_usd(pnl, True)}"
            f"{funding} · stop {_num(stop)} · verdict <b>{verdict}</b> ({who})"
        )

    # --- the pending entry ---------------------------------------------------------------
    if state.entries:
        lines.append("")
        lines.append("<b>Entrée en attente</b>")
        for p in state.entries.values():
            lines.append(
                f"• <b>{_e(p.asset)}</b> {SIDE_FR.get(p.side, p.side)} {p.size:g} · stop-entry "
                f"{_num(p.trigger)} (limite {_num(p.limit_px)}) · stop {_num(p.stop)} · "
                f"objectif {_num(p.target)} · risque {_usd(p.risk)}"
            )

    # --- the traded horizon now -------------------------------------------------------------
    block = (snapshot.get("horizons") or {}).get(ecfg.horizon)
    if block:
        lines.append("")
        lines.append(
            f"<b>Signaux {_e(ecfg.horizon)}</b> (barre du {_e(block.get('generated_at', '?'))})"
        )
        setups = [s for s in block.get("signals", []) if s.get("action") in ("long", "short")]
        pick = next((s for s in setups if s.get("is_top_pick")), None)
        if pick is not None:
            apgar = (pick.get("apgar") or {}).get("total")
            rr = pick.get("reward_risk")
            rr_part = f", R:R {rr:.2f}" if rr is not None else ""
            lines.append(
                f"• Meilleur trade : <b>{_e(pick['asset'])} {_e(pick['action'])}</b> "
                f"(Apgar {apgar}/10{rr_part})"
            )
        else:
            lines.append("• Aucun A-trade.")
        for s in setups:
            if s is pick:
                continue
            apgar = (s.get("apgar") or {}).get("total")
            lines.append(f"• {_e(s['asset'])} {_e(s['action'])} (Apgar {apgar}/10)")
        flagged = sorted({f for s in setups for f in s.get("data_flags", [])})
        if flagged:
            lines.append(f"• ⚠ qualité des données : {_e(', '.join(flagged))}")

    lines.append("")
    lines.append(
        "<i>Chaque entrée est dimensionnée seule à "
        f"{cfg.risk.risk_pct:.1%} de risque ; la règle des 6 % plafonne le risque ouvert. "
        "Outil d'analyse — pas un conseil financier.</i>"
    )
    return "\n".join(lines)
