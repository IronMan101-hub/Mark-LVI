"""Personal finance awareness & tracking. Manual records are labelled MANUAL, market data LIVE.
No trading, no transfers, no bank credentials. Nothing here is ever sent to an AI provider."""
from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime
from typing import Optional

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

HOLDING_KINDS = ["bank_account", "fd", "rd", "mutual_fund", "sip", "stock", "gold", "ppf", "other"]
INDICES = {"sp500": "^spx", "nasdaq": "^ndq", "dow": "^dji", "nikkei": "^nkx", "dax": "^dax", "ftse": "^ukx"}


def fd_maturity(principal: float, rate_pct: float, years: float, compounding: int = 4) -> float:
    return principal * (1 + rate_pct / 100 / compounding) ** (compounding * years)


def rd_maturity(monthly: float, rate_pct: float, months: int) -> float:
    """Quarterly-compounded RD (common Indian convention)."""
    r, total = rate_pct / 100, 0.0
    for m in range(1, months + 1):
        total += monthly * (1 + r / 4) ** (4 * (months - m + 1) / 12)
    return total


def sip_future_value(monthly: float, annual_pct: float, months: int) -> float:
    i = annual_pct / 100 / 12
    return monthly * months if i == 0 else monthly * (((1 + i) ** months - 1) / i) * (1 + i)


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


class Finance(Plugin):
    manifest = Manifest(
        id="finance", name="Finance", category="Finance", version="1.0.0",
        description="Expense/income/savings tracking, budgets, goals, FD/RD/SIP/mutual-fund records (manual), "
                    "market quotes and currency rates (live), and finance news.",
        permissions=["finance.data", "network", "plugins.call", "notify"],
        setup_help="Finance data is private: grant the 'finance.data' permission in Plugins → Finance. You enter your own numbers; "
                   "no bank login is ever requested. Estimates (maturity, SIP growth) are projections, not guarantees.")

    def health(self):
        return Health("ok", "local ledger ready; market data from Stooq / ECB (Frankfurter), no key needed")

    @staticmethod
    def _day(value: str) -> str:
        v = (value or "").strip().lower()
        if not v or v == "today":
            return date.today().isoformat()
        if v == "yesterday":
            return date.fromordinal(date.today().toordinal() - 1).isoformat()
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise PluginError("Date must look like 2026-11-07 (or 'today' / 'yesterday').")

    # ── ledger ────────────────────────────────────────────────────────
    @command(description="Record an expense, income or savings deposit (manually entered).", permissions=("finance.data",),
             params={"kind": {"type": "string", "enum": ["expense", "income", "saving"], "required": True},
                     "amount": {"type": "number", "required": True}, "category": {"type": "string", "default": "general"},
                     "note": {"type": "string"}, "date": {"type": "string", "default": "today"}})
    def add_entry(self, kind, amount, category="general", note="", date="today"):
        if amount <= 0 or amount > 1e10:
            raise PluginError("Amount must be a positive number.")
        day = self._day(date)
        eid = self.ctx.db.execute("INSERT INTO finance_entries(kind,amount,category,note,day,source,created) VALUES(?,?,?,?,?,?,?)",
                                  (kind, round(amount, 2), category.strip().lower(), note, day, "manual", time.time()))
        msg = f"Recorded {kind} of {amount:,.2f} in '{category}' on {day} (entry #{eid}, manual)."
        if kind == "expense":
            msg += self._budget_check(category.strip().lower(), day[:7])
        return {"message": msg, "data": {"id": eid}}

    def _budget_check(self, category: str, month: str) -> str:
        b = self.ctx.db.one("SELECT monthly_limit FROM finance_budgets WHERE category=?", (category,))
        if not b:
            return ""
        lo, hi = _month_bounds(month)
        spent = self.ctx.db.one("SELECT COALESCE(SUM(amount),0) s FROM finance_entries WHERE kind='expense' AND category=? AND day>=? AND day<?",
                                (category, lo, hi))["s"]
        pct = spent / b["monthly_limit"] * 100
        if pct >= 80:
            self.ctx.notify(f"Budget alert: {category}", f"{pct:.0f}% of the {b['monthly_limit']:,.0f} budget used ({spent:,.0f}).",
                            "system", 2 if pct < 100 else 3, dedupe_key=f"budget-{category}-{month}-{int(pct // 10)}", dedupe_window=86400)
        return f" Budget '{category}': {spent:,.0f} of {b['monthly_limit']:,.0f} ({pct:.0f}%)."

    @command(description="List ledger entries, newest first.", permissions=("finance.data",), readonly=True,
             params={"month": {"type": "string", "description": "YYYY-MM"}, "kind": {"type": "string", "enum": ["expense", "income", "saving"]},
                     "category": {"type": "string"}, "limit": {"type": "integer", "default": 30, "maximum": 500}})
    def list_entries(self, month="", kind="", category="", limit=30):
        sql, p = "SELECT * FROM finance_entries WHERE 1=1", []
        if month:
            lo, hi = _month_bounds(month); sql += " AND day>=? AND day<?"; p += [lo, hi]
        if kind:
            sql += " AND kind=?"; p.append(kind)
        if category:
            sql += " AND category=?"; p.append(category.lower())
        sql += " ORDER BY day DESC, id DESC LIMIT ?"; p.append(limit)
        rows = self.ctx.db.query(sql, p)
        return {"message": f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}.", "data": {"entries": rows}}

    @command(description="Edit a ledger entry.", permissions=("finance.data",),
             params={"id": {"type": "integer", "required": True}, "amount": {"type": "number"}, "category": {"type": "string"},
                     "note": {"type": "string"}, "date": {"type": "string"}})
    def edit_entry(self, id, amount=None, category="", note="", date=""):
        if not self.ctx.db.one("SELECT id FROM finance_entries WHERE id=?", (id,)):
            raise PluginError(f"No entry #{id}.")
        sets, vals = [], []
        if amount is not None:
            if amount <= 0: raise PluginError("Amount must be positive.")
            sets.append("amount=?"); vals.append(amount)
        if category: sets.append("category=?"); vals.append(category.lower())
        if note: sets.append("note=?"); vals.append(note)
        if date: sets.append("day=?"); vals.append(self._day(date))
        if not sets: raise PluginError("Nothing to change.")
        self.ctx.db.execute(f"UPDATE finance_entries SET {', '.join(sets)} WHERE id=?", (*vals, id))
        return f"Entry #{id} updated."

    @command(description="Delete a ledger entry.", permissions=("finance.data",), params={"id": {"type": "integer", "required": True}},
             confirm="Delete finance entry #{id}?", destructive=True)
    def delete_entry(self, id):
        if not self.ctx.db.rowcount("DELETE FROM finance_entries WHERE id=?", (id,)):
            raise PluginError(f"No entry #{id}.")
        return f"Entry #{id} deleted."

    @command(description="Monthly summary: income, expenses, savings, net, spending by category and budget status.",
             permissions=("finance.data",), readonly=True, params={"month": {"type": "string", "description": "YYYY-MM, default this month"}})
    def summary(self, month=""):
        month = month or date.today().strftime("%Y-%m")
        lo, hi = _month_bounds(month)
        db = self.ctx.db
        tot = {r["kind"]: r["s"] for r in db.query("SELECT kind, SUM(amount) s FROM finance_entries WHERE day>=? AND day<? GROUP BY kind", (lo, hi))}
        cats = db.query("SELECT category, SUM(amount) s FROM finance_entries WHERE kind='expense' AND day>=? AND day<? GROUP BY category ORDER BY s DESC", (lo, hi))
        budgets = []
        for b in db.query("SELECT * FROM finance_budgets ORDER BY category"):
            spent = next((c["s"] for c in cats if c["category"] == b["category"]), 0)
            budgets.append({"category": b["category"], "limit": b["monthly_limit"], "spent": spent, "percent": round(spent / b["monthly_limit"] * 100, 1)})
        inc, exp, sav = tot.get("income", 0), tot.get("expense", 0), tot.get("saving", 0)
        total_saved = db.one("SELECT COALESCE(SUM(amount),0) s FROM finance_entries WHERE kind='saving'")["s"]
        data = {"month": month, "income": inc, "expenses": exp, "savings_added": sav, "net": inc - exp - sav,
                "categories": cats, "budgets": budgets, "total_recorded_savings": total_saved, "source": "manual entries"}
        return {"message": f"{month}: income {inc:,.0f}, expenses {exp:,.0f}, saved {sav:,.0f}, net {inc - exp - sav:,.0f}."
                           + (f" Top spend: {cats[0]['category']} ({cats[0]['s']:,.0f})." if cats else "") + " (manual records)", "data": data}

    @command(description="Set a monthly budget for a category.", permissions=("finance.data",),
             params={"category": {"type": "string", "required": True}, "monthly_limit": {"type": "number", "required": True, "minimum": 1}})
    def set_budget(self, category, monthly_limit):
        self.ctx.db.execute("INSERT INTO finance_budgets(category,monthly_limit) VALUES(?,?) ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
                            (category.lower(), monthly_limit))
        return f"Budget for '{category}' set to {monthly_limit:,.0f} per month."

    @command(description="Create a savings goal, or add progress to one.", permissions=("finance.data",),
             params={"name": {"type": "string", "required": True}, "target": {"type": "number"}, "add_saved": {"type": "number"}, "deadline": {"type": "string"}})
    def goal(self, name, target=None, add_saved=None, deadline=""):
        row = self.ctx.db.one("SELECT * FROM finance_goals WHERE name=? COLLATE NOCASE", (name,))
        if not row:
            if not target or target <= 0:
                raise PluginError("A new goal needs a target amount.")
            self.ctx.db.execute("INSERT INTO finance_goals(name,target,saved,deadline,created) VALUES(?,?,?,?,?)",
                                (name, target, add_saved or 0, deadline or None, time.time()))
            return f"Goal '{name}' created: target {target:,.0f}."
        if add_saved:
            self.ctx.db.execute("UPDATE finance_goals SET saved=saved+? WHERE id=?", (add_saved, row["id"]))
        if target:
            self.ctx.db.execute("UPDATE finance_goals SET target=? WHERE id=?", (target, row["id"]))
        r = self.ctx.db.one("SELECT * FROM finance_goals WHERE id=?", (row["id"],))
        return f"Goal '{name}': {r['saved']:,.0f} of {r['target']:,.0f} ({r['saved'] / r['target'] * 100:.0f}%)."

    @command(description="List savings goals.", permissions=("finance.data",), readonly=True)
    def goals(self):
        rows = self.ctx.db.query("SELECT * FROM finance_goals ORDER BY created")
        for r in rows:
            r["percent"] = round(r["saved"] / r["target"] * 100, 1)
        return {"message": f"{len(rows)} goal(s)." + "".join(f" {r['name']} {r['percent']}%." for r in rows[:5]), "data": {"goals": rows}}

    # ── holdings (FD / RD / SIP / mutual fund / accounts) ─────────────
    @command(description="Record a holding you track manually: bank_account, fd, rd, mutual_fund, sip, stock, gold, ppf, other. "
                          "For fd/rd/sip give rate and dates so projections can be computed.",
             permissions=("finance.data",),
             params={"kind": {"type": "string", "enum": HOLDING_KINDS, "required": True}, "name": {"type": "string", "required": True},
                     "amount": {"type": "number", "required": True, "description": "Principal / current value"},
                     "rate": {"type": "number", "description": "Annual interest or expected return %"},
                     "start": {"type": "string"}, "maturity": {"type": "string"}, "monthly": {"type": "number", "description": "Monthly instalment (rd/sip)"},
                     "note": {"type": "string"}})
    def add_holding(self, kind, name, amount, rate=None, start="", maturity="", monthly=None, note=""):
        hid = self.ctx.db.execute("INSERT INTO finance_holdings(kind,name,amount,rate,start,maturity,monthly,note,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (kind, name, amount, rate, start or None, maturity or None, monthly, note, time.time()))
        return {"message": f"Recorded {kind.replace('_', ' ')} '{name}' ({amount:,.0f}, manual).", "data": {"id": hid}}

    @command(description="Update a holding's value or details.", permissions=("finance.data",),
             params={"id": {"type": "integer", "required": True}, "amount": {"type": "number"}, "rate": {"type": "number"},
                     "monthly": {"type": "number"}, "maturity": {"type": "string"}, "note": {"type": "string"}})
    def update_holding(self, id, amount=None, rate=None, monthly=None, maturity="", note=""):
        if not self.ctx.db.one("SELECT id FROM finance_holdings WHERE id=?", (id,)):
            raise PluginError(f"No holding #{id}.")
        sets, vals = ["updated=?"], [time.time()]
        for col, v in (("amount", amount), ("rate", rate), ("monthly", monthly), ("maturity", maturity or None), ("note", note or None)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        self.ctx.db.execute(f"UPDATE finance_holdings SET {', '.join(sets)} WHERE id=?", (*vals, id))
        return f"Holding #{id} updated."

    @command(description="Delete a holding.", permissions=("finance.data",), params={"id": {"type": "integer", "required": True}},
             confirm="Delete holding #{id}?", destructive=True)
    def delete_holding(self, id):
        if not self.ctx.db.rowcount("DELETE FROM finance_holdings WHERE id=?", (id,)):
            raise PluginError(f"No holding #{id}.")
        return "Deleted."

    @command(description="Portfolio overview of all manually tracked holdings with projected maturity values (estimates).",
             permissions=("finance.data",), readonly=True)
    def holdings(self):
        rows = self.ctx.db.query("SELECT * FROM finance_holdings ORDER BY kind, name")
        today = date.today()
        total = 0.0
        for r in rows:
            total += r["amount"]
            r["source"] = "manual"
            r["projection"] = None
            try:
                if r["kind"] == "fd" and r["rate"] and r["start"] and r["maturity"]:
                    yrs = (date.fromisoformat(r["maturity"]) - date.fromisoformat(r["start"])).days / 365
                    r["projection"] = {"maturity_value": round(fd_maturity(r["amount"], r["rate"], yrs), 2), "years": round(yrs, 2),
                                       "days_left": (date.fromisoformat(r["maturity"]) - today).days, "estimate": True}
                elif r["kind"] == "rd" and r["rate"] and r["monthly"] and r["start"] and r["maturity"]:
                    months = round((date.fromisoformat(r["maturity"]) - date.fromisoformat(r["start"])).days / 30.4)
                    r["projection"] = {"maturity_value": round(rd_maturity(r["monthly"], r["rate"], months), 2), "months": months, "estimate": True}
                elif r["kind"] == "sip" and r["rate"] and r["monthly"]:
                    months = 120 if not r["maturity"] else max(1, round((date.fromisoformat(r["maturity"]) - today).days / 30.4))
                    r["projection"] = {"future_value": round(sip_future_value(r["monthly"], r["rate"], months), 2), "months": months,
                                       "assumed_return_pct": r["rate"], "estimate": True}
            except ValueError:
                r["projection"] = {"error": "dates must be YYYY-MM-DD"}
        return {"message": f"{len(rows)} holding(s), total recorded value {total:,.0f} (manual; projections are estimates).",
                "data": {"holdings": rows, "total": total}}

    # ── live market data ──────────────────────────────────────────────
    @command(description="Latest quote for a stock/ETF/index symbol (Stooq, delayed). Examples: aapl.us, msft.us, ^spx, reliance.in.",
             permissions=("network",), readonly=True, params={"symbol": {"type": "string", "required": True}})
    def quote(self, symbol):
        sym = INDICES.get(symbol.lower(), symbol.lower())
        try:
            r = self.ctx.http_get("https://stooq.com/q/l/", params={"s": sym, "f": "sd2t2ohlcv", "h": "", "e": "csv"})
            rows = list(csv.DictReader(io.StringIO(r.text)))
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"The market-data service didn't respond ({type(e).__name__}).")
        if not rows or rows[0].get("Close") in (None, "N/D", ""):
            raise PluginError(f"No data for '{symbol}'. Stooq uses suffixes like .us (aapl.us) or .in; indexes look like ^spx.")
        q = rows[0]
        o, c = float(q["Open"]), float(q["Close"])
        chg = (c - o) / o * 100 if o else 0.0
        return {"message": f"{q['Symbol']}: {c:,.2f} ({chg:+.2f}% vs open) — LIVE/delayed, {q['Date']} {q['Time']} (Stooq)",
                "data": {"symbol": q["Symbol"], "close": c, "open": o, "high": float(q["High"]), "low": float(q["Low"]),
                         "change_pct": round(chg, 2), "date": q["Date"], "time": q["Time"], "source": "stooq (live, delayed)"}}

    @command(description="Major index snapshot (S&P 500, Nasdaq, Dow, Nikkei, DAX, FTSE).", permissions=("network",), readonly=True, timeout=40)
    def markets(self):
        out, lines = [], []
        for name in INDICES:
            try:
                q = self.quote(name)
                out.append({"name": name, **q["data"]}); lines.append(f"{name} {q['data']['close']:,.0f} ({q['data']['change_pct']:+.1f}%)")
            except PluginError:
                continue
        if not out:
            raise PluginError("Market data isn't reachable right now.")
        return {"message": "; ".join(lines) + " — live/delayed (Stooq)", "data": {"indices": out}}

    @command(description="Foreign-exchange rate (European Central Bank via Frankfurter).", permissions=("network",), readonly=True,
             params={"base": {"type": "string", "default": "USD"}, "targets": {"type": "array", "description": "e.g. ['INR','EUR']"}})
    def fx(self, base="USD", targets=None):
        targets = [t.upper() for t in (targets or ["INR", "EUR", "GBP"])]
        r = self.ctx.http_get("https://api.frankfurter.app/latest", params={"from": base.upper(), "to": ",".join(targets)})
        if r.status_code != 200:
            raise PluginError("Unknown currency code or the rate service is unavailable.")
        j = r.json()
        return {"message": f"1 {base.upper()} = " + ", ".join(f"{v:,.4f} {k}" for k, v in j["rates"].items()) + f" (ECB, {j['date']})",
                "data": {"base": base.upper(), "rates": j["rates"], "date": j["date"], "source": "ECB via Frankfurter (live)"}}

    @command(description="Finance news headlines.", permissions=("network", "plugins.call"), readonly=True, timeout=120)
    def news(self):
        r = self.ctx.call("news", "headlines", {"category": "finance", "limit": 6})
        return {"ok": r.ok, "message": r.message, "data": r.data}

    @command(description="Dashboard data: savings, month summary, goals, holdings total.", permissions=("finance.data",), readonly=True)
    def dashboard(self):
        s = self.summary()["data"]
        goals = self.goals()["data"]["goals"]
        h = self.holdings()["data"]
        return {"message": f"Savings recorded {s['total_recorded_savings']:,.0f}; this month spent {s['expenses']:,.0f} of income {s['income']:,.0f}.",
                "data": {"summary": s, "goals": goals, "holdings_total": h["total"], "holdings": h["holdings"]}}


PLUGINS = [Finance]
