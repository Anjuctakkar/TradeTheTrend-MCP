"""Run from your project folder (the one with server.py):  python check_install.py
Confirms the support/resistance update is in place and nothing stale is shadowing it."""
import os, sys

here = os.path.dirname(os.path.abspath(__file__))
checks = [
    ("sr_model.py", "def generate_sr_signal"),
    ("sectors.py", "def map_status"),
    (os.path.join("static", "index.html"), "view-sectors"),
    ("server.py", "import sr_model"),
    ("server.py", "signal_model"),
    ("technical.py", "def compare_signals"),
    ("holding.py", "model: str"),
    ("recommend.py", "MODELS ="),
    ("backtest.py", "signal_fn"),
    ("agent_tools.py", "sr_levels"),
    ("agent.py", "sr_levels"),
    (os.path.join("static", "index.html"), "S/R LONG SETUP"),
    (os.path.join("static", "index.html"), "S/R build 2026-10-16.9"),
    (os.path.join("static", "index.html"), "discRun"),
    (os.path.join("static", "index.html"), "secRotation"),
    ("agent_tools.py", "sector_rotation"),
    ("agent_tools.py", "sector_picks"),
    (os.path.join("static", "index.html"), "secPicks"),
    (os.path.join("static", "eval-store.js"), 'model === "sr"'),
    (os.path.join("static", "agent-widget.js"), "Which sectors are improving?"),
    (os.path.join("static", "agent-widget.js"), "function showUsed"),
]
bad = 0
for rel, needle in checks:
    p = os.path.join(here, rel)
    try:
        ok = needle in open(p, encoding="utf-8").read()
    except FileNotFoundError:
        ok = False
    print(("OK    " if ok else "MISSING"), rel, "->", needle)
    bad += not ok
for stray in ("index.html", "eval-store.js", "agent-widget.js"):
    if os.path.exists(os.path.join(here, stray)):
        print("WARNING:", stray, "sits next to server.py. The server only serves static/" + stray, "- delete the stray copy.")
        bad += 1
print("\nAll good. Restart the server, then hard-refresh the page (Ctrl+F5)." if not bad else "\nFix the items above, then run this again.")
sys.exit(1 if bad else 0)
