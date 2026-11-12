"""Read-only trade summary from confirmed receipts and the worker's latest market sample."""
from datetime import datetime, timezone
from decimal import Decimal
import time

import wallet_trades


def summary(db, row, stages):
    if row is None:
        return "لوحة الصفقة · ما في صفقة حقيقية مسجّلة بعد."
    stage = row["stage"]
    lines = ["لوحة الصفقة · " + stages[stage], row["mint"], f"عدد المحافظ المختار: {row['wallet_count']}",
             f"سقف العملية: {row['budget']/1e9:g} SOL  |  هدف البيع: +{row['target']/100:g}%"]
    legs = db.execute("""SELECT COUNT(*), COALESCE(SUM(units),0), COUNT(NULLIF(units,0))
                         FROM child_trades WHERE campaign=?""", (row["id"],)).fetchone()
    if not legs[0]:
        if stage != "plan50":
            lines.append(f"تقدّم المرحلة: {row['cursor']}/{row['wallet_count']}")
        return "\n".join(lines + [row["error"] or "بانتظار بيانات الصفقة."])
    count, units, holders = legs
    lines.append(f"محافظ معها العملة: {holders}/{count}")
    if stage in ("fund50", "buy50", "sell50"):
        lines.append(f"تقدّم الدفعات: {row['cursor']}/{count}")
    basis, fees = db.execute("""SELECT COALESCE(SUM(CASE WHEN stage IN ('fund50','buy50')
                                THEN payer_delta ELSE 0 END),0), COALESCE(SUM(network_fee),0)
                                FROM live_receipts WHERE campaign=?""", (row["id"],)).fetchone()
    if basis:
        lines.append(f"تكلفة الدخول المؤكدة: {basis/1e9:.6f} SOL")
    if stage == "done" and not units:
        profit = -row["spent"]
        if basis > 0:
            lines.append(f"نتيجة العملية قبل التجميع: {profit/1e9:+.6f} SOL ({profit*100/basis:+.2f}%)")
    elif units and basis > 0:
        sample = db.execute("SELECT * FROM market_samples WHERE campaign=?", (row["id"],)).fetchone()
        if sample:
            net = wallet_trades.sale_estimate(int(sample["tokens"]), int(sample["sol"]), units) - holders * 10000
            net = max(0, net)
            profit = net - row["spent"]
            stamp = datetime.fromtimestamp(sample["observed"], timezone.utc).strftime("%H:%M:%S UTC")
            stale = time.time() - sample["observed"] > 30
            lines += [f"الكمية المتبقية: {Decimal(units).scaleb(-row['decimals']):f}",
                      f"قيمة بيع المتبقي التقديرية: {net/1e9:.6f} SOL",
                      f"الربح/الخسارة التقديرية: {profit*100/basis:+.2f}% ({profit/1e9:+.6f} SOL)",
                      ("آخر تسعير قديم: " if stale else "آخر تسعير: ") + stamp]
        else:
            lines.append("بانتظار تسعير الكمية المتبقية.")
    if fees:
        lines.append(f"رسوم الشبكة المؤكدة: {fees/1e9:.6f} SOL")
    if row["manual"] and stage != "done":
        lines.append("طلب البيع اليدوي مسجّل.")
    if row["pending"]:
        lines.append("دفعة بانتظار تأكيد الشبكة.")
    if row["error"]:
        lines.append(row["error"])
    return "\n".join(lines)
