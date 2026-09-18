"""Draw docs/architecture.png. Kept as code so the picture can be regenerated when the pipeline
changes instead of drifting away from it (the previous image still said 46 tests and had no diff stage).

    python tools/architecture_png.py            # -> docs/architecture.png (1600x900)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 900
BG, PANEL, BORDER = (13, 17, 23), (22, 27, 34), (58, 66, 80)
FG, MUTED, BLUE, ORANGE, RED = (230, 237, 243), (140, 150, 160), (88, 166, 255), (255, 190, 70), (255, 95, 95)
FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(f"{FONT_DIR}/DejaVuSans{'-Bold' if bold else ''}.ttf", size)


def panel(d: ImageDraw.ImageDraw, box, border=BORDER, fill=PANEL, width=2):
    d.rounded_rectangle(box, radius=10, fill=fill, outline=border, width=width)


def lines(d: ImageDraw.ImageDraw, x, y, rows, f, fill=FG, gap=6):
    for r in rows:
        d.text((x, y), r, font=f, fill=fill)
        y += f.size + gap
    return y


def main(out: Path = Path("docs/architecture.png")) -> Path:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    d.text((60, 42), "x-metrics-pipeline — what the agent does, what catches it, what moved since last week",
           font=font(27, True), fill=FG)
    lines(d, 60, 92, ["Scrolling a timeline is the easy part. The value is everything after: evidence kept, numbers recomputed,",
                      "model output checked before it reaches a client — and every run compared with the one before it."],
          font(16), fill=MUTED, gap=4)

    # ---- stage boxes: 6 across
    stages = [
        ("collect", ["Playwright, logged-in", "session reuse", "rate limit · retry · resume", "raw aria-label stored"], BORDER),
        ("store", ["SQLite", "accounts · runs", "measurements · posts", "agent_audit"], BORDER),
        ("classify", ["Claude proposes", "niche · spam · confidence", "+ verbatim evidence quotes"], ORANGE),
        ("validate", ["recompute every rate", "min posts · recency", "contact · duplicates", "error → excluded"], BORDER),
        ("export", ["client CSV", "dashboard.html", "validation_report.md", "agent_audit.json"], BORDER),
        ("diff", ["this run vs the last", "followers · engagement", "tier · went silent", "flagged · new · dropped"], BLUE),
    ]
    n, gap, x0, y0, bh = len(stages), 26, 60, 165, 170
    bw = (W - 2 * x0 - gap * (n - 1)) // n
    f_title, f_body = font(22, True), font(14)
    for i, (name, body, border) in enumerate(stages):
        x = x0 + i * (bw + gap)
        panel(d, (x, y0, x + bw, y0 + bh), border=border, width=3 if border != BORDER else 2)
        d.text((x + 16, y0 + 14), name, font=f_title, fill=ORANGE if border == ORANGE else BLUE)
        lines(d, x + 16, y0 + 50, body, f_body, gap=5)
        if i < n - 1:
            ax = x + bw + 4
            d.line((ax, y0 + bh // 2, ax + gap - 8, y0 + bh // 2), fill=MUTED, width=3)
            d.polygon([(ax + gap - 8, y0 + bh // 2 - 6), (ax + gap - 8, y0 + bh // 2 + 6), (ax + gap - 1, y0 + bh // 2)], fill=MUTED)

    # connector from classify to guard panel
    cx = x0 + 2 * (bw + gap) + bw // 2
    d.line((cx, y0 + bh, cx, 395), fill=ORANGE, width=3)
    # connector from diff to changes panel
    dx = x0 + 5 * (bw + gap) + bw // 2
    d.line((dx, y0 + bh, dx, 395), fill=BLUE, width=3)

    # ---- bottom-left: live run vs hand sheet
    L = (60, 395, 640, 850)
    panel(d, L)
    d.text((78, 412), "live run 2026-09-06 vs hand-measured sheet", font=font(19, True), fill=BLUE)
    ft, fb = font(15, True), font(15)
    d.text((78, 460), "account", font=ft, fill=MUTED); d.text((290, 460), "pipeline ER · VR", font=ft, fill=MUTED); d.text((470, 460), "hand (09-05)", font=ft, fill=MUTED)
    rows = [("@realFatCat1", "0.75% · 41.6%", "0.70% · 39.4%"), ("@merrittblack", "0.26% · 23.8%", "0.27% · 28.7%"), ("@ProbableChris", "0.70% · 60.1%", "0.72% · 58.7%")]
    y = 498
    for a, b, c in rows:
        d.text((78, y), a, font=fb, fill=FG); d.text((290, y), b, font=fb, fill=FG); d.text((470, y), c, font=fb, fill=FG); y += 36
    lines(d, 78, y + 18, ["3 accounts · 20 original posts each · 36 s",
                          "classify: 3/3 accepted (every quote verbatim)",
                          "validate: 0 errors · 53 tests passing",
                          "weekly GitHub Actions: tests → validate → export → diff",
                          "MCP server · n8n workflow",
                          "diff: second week not measured yet — README example is the test fixture"],
          font(14), fill=MUTED, gap=5)

    # ---- bottom-middle: guard panel
    G = (665, 395, 1225, 850)
    panel(d, G, border=ORANGE, fill=(31, 27, 20), width=3)
    d.text((683, 412), "guard()  — deterministic, model-agnostic", font=font(19, True), fill=ORANGE)
    checks = [("schema", "not JSON · missing keys · confidence ∉ 0–1", "rejected", RED),
              ("label_set", "invented category outside the closed set", "rejected", RED),
              ("evidence", "quote not a verbatim substring of bio/posts", "rejected", RED),
              ("confidence", "below 0.7", "review queue", ORANGE),
              ("rule_conflict", "model and rule-based spam screen disagree", "review queue", ORANGE)]
    y = 462
    for k, what, verdict, col in checks:
        d.text((683, y), k, font=font(15, True), fill=FG)
        d.text((800, y), what, font=font(12), fill=MUTED)
        d.text((1110, y), verdict, font=font(14, True), fill=col)
        y += 38
    lines(d, 683, 690, ["Only 'accepted' reaches the deliverable. Every attempt is logged",
                        "with the raw output and which checks fired — that audit table is",
                        "the README's 'what the agent got wrong'. Tests feed deliberately",
                        "wrong model answers and assert each one is caught."],
          font(13), fill=FG, gap=5)

    # ---- bottom-right: changes panel
    C = (1250, 395, 1540, 850)
    panel(d, C, border=BLUE, fill=(18, 26, 38), width=3)
    d.text((1268, 412), "changes.md", font=font(19, True), fill=BLUE)
    lines(d, 1268, 455, ["each account, this run vs last:", "", "flagged  crossed a threshold", "   followers ±5%", "   engagement 0.2 pp and 25%",
                         "   views/followers ±25%", "   silent ≥ 14 days", "   tier change — always",
                         "new       first time measured", "dropped  in last run, not this",
                         "", "every other delta shown too,", "unflagged. Nothing estimated:", "two stored rows, one subtraction.",
                         "", "--fail-on-flags → page someone", "only when something moved."],
          font(13), fill=FG, gap=5)

    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


if __name__ == "__main__":
    print(main())
