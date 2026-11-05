"""Summarise a replay CSV: whole period, both halves, by strategy / symbol / month.
Usage: python summary.py CSV [--by strategy|symbol|month|reason] [--split YYYY-MM-DD]"""
import csv
import sys
from collections import defaultdict

path = sys.argv[1]
by = sys.argv[sys.argv.index("--by") + 1] if "--by" in sys.argv else None
rows = list(csv.DictReader(open(path)))
for r in rows:
    r["inr"] = float(r["net_inr"]); r["R"] = float(r["r"]); r["month"] = r["day"][:7]
rows.sort(key=lambda r: r["opened_ist"])
days = sorted({r["day"] for r in rows})
split = sys.argv[sys.argv.index("--split") + 1] if "--split" in sys.argv else (days[len(days) // 2] if days else "")


def line(name, rs):
    if not rs:
        return f"{name:<26}   0 trades"
    n = len(rs); w = sum(1 for r in rs if r["inr"] > 0)
    tot = sum(r["inr"] for r in rs)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r["inr"]; peak = max(peak, eq); dd = min(dd, eq - peak)
    return (f"{name:<26}{n:5d} trades  win {100*w/n:4.0f}%  avgR {sum(r['R'] for r in rs)/n:+.2f}  "
            f"₹{tot:+9,.0f}  per trade ₹{tot/n:+6.0f}  maxDD ₹{dd:,.0f}")


def block(rs, title):
    print(f"── {title}")
    print("  " + line("ALL", rs))
    print("  " + line(f"H1 (< {split})", [r for r in rs if r["day"] < split]))
    print("  " + line(f"H2 (>= {split})", [r for r in rs if r["day"] >= split]))


block(rows, f"{path}  {days[0] if days else ''} .. {days[-1] if days else ''}")
if by:
    g = defaultdict(list)
    for r in rows:
        g[r[by]].append(r)
    for k in sorted(g):
        h1 = sum(r["inr"] for r in g[k] if r["day"] < split); h2 = sum(r["inr"] for r in g[k] if r["day"] >= split)
        print("  " + line(k, g[k]) + f"  | H1 ₹{h1:+,.0f} H2 ₹{h2:+,.0f}")
