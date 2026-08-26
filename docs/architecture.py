"""Regenerates architecture.png and architecture_simple.png.

The diagrams used to be binaries with no source, which is how the full one
drifted: it claimed "17+ rules, zero LLM dependency" long after the engine had
settled at 14 rules and the README had a whole section explaining that two of
them *do* read model output. A diagram nobody can regenerate is a diagram
nobody updates.

Run from the repository root:

    python docs/architecture.py

Requires matplotlib (a docs-only dependency — it is deliberately not in
requirements.txt, since nothing at runtime imports it).
"""

import os

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# ── Palette ──────────────────────────────────────────────────────────────
# The legend at the bottom of the full diagram is load-bearing: blue means a
# model produced this, gray means code did, orange is the gate between them.
BG = "#0d0d0d"
TEXT = "#ffffff"
MUTED = "#9aa0a6"

LLM_FILL, LLM_EDGE = "#2f7ded", "#8ab4ff"
CODE_FILL, CODE_EDGE = "#4a5058", "#d0d4d8"
POLICY_FILL, POLICY_EDGE = "#f0a020", "#ffd27f"
ALLOW_FILL, ALLOW_EDGE = "#22c55e", "#bbf7d0"
WORKER_FILL, WORKER_EDGE = "#15803d", "#86efac"
BLOCK_FILL, BLOCK_EDGE = "#5f1414", "#ef4444"
HUMAN_FILL, HUMAN_EDGE = "#5a3a0a", "#f59e0b"
OUT_FILL, OUT_EDGE = "#14263d", "#38bdf8"
HEALTH_FILL, HEALTH_EDGE = "#312e81", "#818cf8"
RECOVERY_FILL, RECOVERY_EDGE = "#3f1d38", "#c084fc"
REVIEW_FILL, REVIEW_EDGE = "#3d2f0a", "#fbbf24"
CAPITAL_FILL, CAPITAL_EDGE = "#3d3016", "#ffd65c"


def box(ax, x, y, w, h, title, sub=None, fill=CODE_FILL, edge=CODE_EDGE,
        title_size=13, sub_size=9, title_color=TEXT, sub_color=None, radius=0.02):
    """One rounded node. `sub` is the small second line inside the box."""
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        facecolor=fill, edgecolor=edge, linewidth=1.6, zorder=2,
    ))
    if sub:
        ax.text(x + w / 2, y + h * 0.62, title, ha="center", va="center",
                color=title_color, fontsize=title_size, fontweight="bold", zorder=3)
        ax.text(x + w / 2, y + h * 0.27, sub, ha="center", va="center",
                color=sub_color or MUTED, fontsize=sub_size, zorder=3)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center",
                color=title_color, fontsize=title_size, fontweight="bold", zorder=3)


def stack(ax, x, y, w, h, lines, fill=CODE_FILL, edge=CODE_EDGE, radius=0.02):
    """A node whose contents are an explicit list of (text, size, colour, bold).

    Used wherever a box needs more than a title and one subtitle. Laying the
    lines out from a list is what keeps them from landing on top of each other
    when a label grows — which is exactly how the previous diagram ended up
    with its rule count printed over its own subtitle.
    """
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        facecolor=fill, edgecolor=edge, linewidth=1.6, zorder=2,
    ))
    n = len(lines)
    top, bottom = y + h * 0.80, y + h * 0.20
    step = (top - bottom) / (n - 1) if n > 1 else 0
    for i, (text, size, color, bold) in enumerate(lines):
        ax.text(x + w / 2, top - i * step, text, ha="center", va="center",
                color=color, fontsize=size,
                fontweight="bold" if bold else "normal", zorder=3)


def arrow(ax, start, end, color=TEXT, rad=0.0, lw=2.0, style="-|>", dashed=False):
    ax.add_patch(FancyArrowPatch(
        start, end,
        connectionstyle=f"arc3,rad={rad}",
        arrowstyle=style, mutation_scale=18,
        color=color, linewidth=lw, zorder=1,
        linestyle="--" if dashed else "-",
    ))


def new_canvas(width, height, bottom=0.0):
    """A 0..1 drawing area. `bottom` extends it downward for a trailing row —
    boxes placed below y=0 are otherwise silently clipped, which is how the
    capital-lead panel first rendered as floating text with no box around it."""
    fig, ax = plt.subplots(figsize=(width, height))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(bottom, 1)
    ax.axis("off")
    return fig, ax


# ─────────────────────────────────────────────────────────────────────────
# Full architecture
# ─────────────────────────────────────────────────────────────────────────

def draw_full(path):
    fig, ax = new_canvas(19.8, 15.2, bottom=-0.20)

    ax.text(0.5, 0.972, "RecoverRight — Architecture", ha="center",
            color=TEXT, fontsize=27, fontweight="bold")
    ax.text(0.5, 0.945,
            "Overdue-invoice recovery pipeline: LLM reasoning gated by a deterministic policy engine",
            ha="center", color=MUTED, fontsize=11.5, style="italic")

    # ── Input layer ──────────────────────────────────────────────────────
    ax.text(0.100, 0.905, "INPUT LAYER", ha="center", color=MUTED,
            fontsize=10.5, fontweight="bold")
    inputs = [
        ("Razorpay API Sync", 0.826, LLM_FILL, LLM_EDGE),
        ("CSV Upload", 0.746, CODE_FILL, CODE_EDGE),
        ("JSON Batch", 0.666, CODE_FILL, CODE_EDGE),
    ]
    for label, y, fill, edge in inputs:
        box(ax, 0.028, y, 0.145, 0.054, label, fill=fill, edge=edge, title_size=12)
        arrow(ax, (0.173, y + 0.027), (0.296, 0.864), color="#8b8f94", rad=0.18, lw=1.6)

    # ── Diagnosis column ─────────────────────────────────────────────────
    box(ax, 0.296, 0.838, 0.190, 0.052, "Detect At-Risk", "CODE",
        fill=CODE_FILL, edge=CODE_EDGE)
    arrow(ax, (0.391, 0.838), (0.391, 0.798))

    box(ax, 0.296, 0.740, 0.190, 0.058, "Signal Extraction", "LLM Call 1",
        fill=LLM_FILL, edge=LLM_EDGE, sub_color="#dbeafe")
    arrow(ax, (0.391, 0.740), (0.391, 0.700))

    box(ax, 0.296, 0.628, 0.190, 0.072, "ReAct Reasoning", "LLM Call 2",
        fill=LLM_FILL, edge=LLM_EDGE, sub_color="#dbeafe")

    # The five thoughts, as a chain hanging off call 2.
    ax.text(0.610, 0.700, "5 reasoning steps per invoice", ha="center",
            color="#8ab4ff", fontsize=9.5)
    for i in range(5):
        cx = 0.526 + i * 0.044
        ax.add_patch(plt.Circle((cx, 0.664), 0.0150, facecolor=LLM_FILL,
                                edgecolor=LLM_EDGE, linewidth=1.4, zorder=2))
        ax.text(cx, 0.664, f"T{i + 1}", ha="center", va="center",
                color=TEXT, fontsize=8.5, fontweight="bold", zorder=3)
        if i:
            arrow(ax, (cx - 0.044 + 0.0150, 0.664), (cx - 0.0150, 0.664),
                  color="#8ab4ff", lw=1.2)
    arrow(ax, (0.486, 0.664), (0.5110, 0.664), color="#8ab4ff", lw=1.2)

    # Whitelist note sits to the LEFT of the arrow, not across it.
    ax.text(0.286, 0.600,
            "every field validated against a whitelist —\n"
            "anything unrecognised degrades to the most\n"
            "cautious value before it reaches policy",
            ha="right", va="top", color=MUTED, fontsize=8.6)
    arrow(ax, (0.391, 0.628), (0.391, 0.558))

    # ── Policy engine ────────────────────────────────────────────────────
    # The claim the previous diagram got wrong. There are 14 rules, and 12 of
    # them read no model output at all; the other two can only tighten a
    # decision, never loosen one.
    stack(ax, 0.281, 0.474, 0.226, 0.084, [
        ("Policy Engine", 15, "#1a1200", True),
        ("DETERMINISTIC CODE", 9.5, "#4a3200", False),
        ("14 rules · first-match-wins", 8.6, "#5c3d00", True),
        ("12 of the 14 are model-independent", 8.6, "#5c3d00", True),
    ], fill=POLICY_FILL, edge=POLICY_EDGE)

    ax.text(0.172, 0.462, "ALLOW", ha="center", color="#22c55e",
            fontsize=12.5, fontweight="bold")
    ax.text(0.391, 0.441, "BLOCK", ha="center", color="#ef4444",
            fontsize=12.5, fontweight="bold")
    ax.text(0.686, 0.452, "REQUIRE_HUMAN", ha="center", color="#f59e0b",
            fontsize=12.5, fontweight="bold")

    arrow(ax, (0.281, 0.510), (0.150, 0.420), color="#22c55e", rad=0.25, lw=2.4)
    arrow(ax, (0.391, 0.474), (0.391, 0.420), color="#ef4444", lw=2.4)
    arrow(ax, (0.507, 0.510), (0.623, 0.420), color="#f59e0b", rad=-0.25, lw=2.4)

    # ── Execution layer ──────────────────────────────────────────────────
    box(ax, 0.064, 0.358, 0.172, 0.062, "Orchestrator",
        "deterministic routing · no LLM",
        fill=ALLOW_FILL, edge=ALLOW_EDGE, title_size=13,
        title_color="#052e16", sub_color="#065f28", sub_size=8.2)

    workers = [("Payment Link\nWorker", 0.012), ("Wait\nWorker", 0.098),
               ("Information\nWorker", 0.184)]
    for label, x in workers:
        box(ax, x, 0.252, 0.080, 0.058, label, fill=WORKER_FILL,
            edge=WORKER_EDGE, title_size=9.5)
        arrow(ax, (0.150, 0.358), (x + 0.040, 0.310), color="#22c55e",
              rad=0.12, lw=1.5)
        arrow(ax, (x + 0.040, 0.252), (0.288, 0.196), color="#22c55e",
              rad=-0.10, lw=1.3)

    box(ax, 0.308, 0.358, 0.168, 0.062, "Block Worker", "zero customer action",
        fill=BLOCK_FILL, edge=BLOCK_EDGE, title_size=13, sub_color="#fca5a5")
    arrow(ax, (0.392, 0.358), (0.392, 0.222), color="#ef4444", lw=2.0)

    box(ax, 0.520, 0.358, 0.206, 0.062, "Escalation Worker",
        "priority: URGENT / HIGH / MEDIUM / LOW",
        fill=HUMAN_FILL, edge=HUMAN_EDGE, title_size=12.5, sub_color="#fcd34d",
        sub_size=8.0)

    # The queue the escalation lands in. Its whole safety argument is the line
    # inside it: a reviewer picks from the engine's own allowed_actions.
    stack(ax, 0.520, 0.232, 0.206, 0.086, [
        ("HUMAN REVIEW QUEUE", 11, "#fde68a", True),
        ("approve · reject · defer", 8.6, "#fcd34d", False),
        ("approval picks only from the", 8.0, "#fcd34d", False),
        ("engine's own allowed_actions", 8.0, "#fcd34d", False),
    ], fill=REVIEW_FILL, edge=REVIEW_EDGE)
    arrow(ax, (0.623, 0.358), (0.623, 0.318), color="#f59e0b", lw=2.0)
    arrow(ax, (0.520, 0.258), (0.500, 0.196), color="#f59e0b", rad=0.20, lw=1.8)

    # ── Failure recovery — the layer the old diagram left out entirely ───
    stack(ax, 0.812, 0.556, 0.176, 0.152, [
        ("FAILURE RECOVERY", 10.5, "#e9d5ff", True),
        ("full-jitter backoff", 8.4, "#d8b4fe", False),
        ("shared 429 cooldown", 8.4, "#d8b4fe", False),
        ("adaptive request pacer", 8.4, "#d8b4fe", False),
        ("serial sweep, 2 passes", 8.4, "#d8b4fe", False),
        ("fail closed to a human", 8.4, "#d8b4fe", False),
    ], fill=RECOVERY_FILL, edge=RECOVERY_EDGE)
    # Enters below the thought chain so it crosses nothing.
    arrow(ax, (0.812, 0.596), (0.487, 0.636), color="#c084fc", rad=0.10,
          lw=1.5, dashed=True)
    ax.text(0.655, 0.583, "wraps every LLM call", ha="center",
            color="#c084fc", fontsize=8.6, style="italic")

    # ── Audit + reconcile ────────────────────────────────────────────────
    stack(ax, 0.286, 0.128, 0.212, 0.078, [
        ("Audit Trail", 14, TEXT, True),
        ("PostgreSQL · append-only", 8.6, MUTED, False),
        ("every decision logged, including non-actions", 8.4, MUTED, False),
    ], fill="#2b3038", edge="#c3c8ce")
    arrow(ax, (0.392, 0.128), (0.392, 0.096))

    box(ax, 0.286, 0.020, 0.212, 0.076, "Reconcile",
        "the only writer of amount_recovered_paise",
        fill=CODE_FILL, edge=CODE_EDGE, title_size=14, sub_size=8.2)

    # Two triggers, one writer. The webhook says *when* to look; the looking
    # is the same API re-read, so the delivery's own amount is never used.
    stack(ax, 0.030, 0.148, 0.212, 0.078, [
        ("Razorpay Webhook", 12, TEXT, True),
        ("HMAC-SHA256 verified", 8.2, MUTED, False),
        ("triggers the re-read, never credits", 8.0, MUTED, False),
    ], fill="#2b3038", edge="#c3c8ce")
    arrow(ax, (0.242, 0.172), (0.286, 0.082), color="#c3c8ce", rad=-0.25, lw=1.6)

    # Health scoring runs after the batch, never during it.
    stack(ax, 0.030, 0.028, 0.212, 0.082, [
        ("Client Health Scoring", 12, TEXT, True),
        ("runs after the batch, never during it", 8.0, "#c7d2fe", False),
        ("Healthy | Watch | At-Risk | Critical", 8.2, "#c7d2fe", False),
    ], fill=HEALTH_FILL, edge=HEALTH_EDGE)
    arrow(ax, (0.286, 0.140), (0.180, 0.112), color="#818cf8", rad=0.22,
          lw=1.5, dashed=True)

    # The one output that is revenue for Razorpay rather than for the merchant.
    stack(ax, 0.030, -0.108, 0.290, 0.100, [
        ("RAZORPAY CAPITAL LEAD", 11, "#ffe9a8", True),
        ("cash-flow constrained, not unwilling to pay", 8.2, "#ffd65c", False),
        ("no disputes · no legal threats · was formerly reliable", 8.2, "#ffd65c", False),
        ("a qualified lead — this system makes no credit decision", 8.2, "#ffd65c", False),
    ], fill=CAPITAL_FILL, edge=CAPITAL_EDGE)
    arrow(ax, (0.136, 0.028), (0.160, -0.006), color="#ffd65c", lw=1.8)

    # ── Output layer ─────────────────────────────────────────────────────
    ax.text(0.846, 0.318, "OUTPUT LAYER", ha="center", color=MUTED,
            fontsize=10.5, fontweight="bold")
    box(ax, 0.746, 0.192, 0.200, 0.078, "Dashboard",
        "Invoices | Health | Escalations | Analytics",
        fill=OUT_FILL, edge=OUT_EDGE, title_size=14, sub_size=8.0,
        sub_color="#bae6fd")
    box(ax, 0.746, 0.048, 0.200, 0.078, "Metrics",
        "0 inappropriate actions · 100% block rate",
        fill=OUT_FILL, edge=OUT_EDGE, title_size=14, sub_size=8.0,
        sub_color="#bae6fd")
    arrow(ax, (0.498, 0.174), (0.746, 0.228), color="#38bdf8", rad=-0.12, lw=1.8)
    arrow(ax, (0.498, 0.050), (0.746, 0.086), color="#38bdf8", rad=-0.10, lw=1.8)

    # ── Legend ───────────────────────────────────────────────────────────
    legend = [
        ("Blue = LLM", LLM_EDGE),
        ("Gray = Deterministic Code", CODE_EDGE),
        ("Orange = Policy Engine", POLICY_EDGE),
        ("Amber = Human Review", REVIEW_EDGE),
        ("Purple = Failure Recovery", RECOVERY_EDGE),
    ]
    for i, (label, color) in enumerate(legend):
        ax.text(0.115 + i * 0.192, -0.170, label, ha="center", color=color,
                fontsize=10.5, fontweight="bold")

    fig.savefig(path, facecolor=BG, dpi=120, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────
# Simplified diagram, for the pitch video
# ─────────────────────────────────────────────────────────────────────────

def draw_simple(path):
    fig, ax = new_canvas(12.0, 7.5)

    ax.text(0.5, 0.935, "RecoverRight — How It Works", ha="center",
            color=TEXT, fontsize=17, fontweight="bold")
    ax.text(0.045, 0.862,
            "Legend:   Blue = LLM    Gray = Deterministic Code    Orange = Policy Engine",
            ha="left", color=MUTED, fontsize=7.5)

    box(ax, 0.018, 0.625, 0.190, 0.105, "Razorpay / CSV / JSON",
        fill=LLM_FILL, edge=LLM_EDGE, title_size=9.5)
    arrow(ax, (0.208, 0.677), (0.283, 0.677), lw=2.0)

    box(ax, 0.283, 0.625, 0.165, 0.105, "Detect", "code",
        fill=CODE_FILL, edge=CODE_EDGE, title_size=11, sub_size=7.5)
    arrow(ax, (0.448, 0.677), (0.528, 0.677), lw=2.0)

    box(ax, 0.528, 0.625, 0.165, 0.105, "Reason", "LLM",
        fill=LLM_FILL, edge=LLM_EDGE, title_size=11, sub_size=7.5,
        sub_color="#dbeafe")
    arrow(ax, (0.610, 0.625), (0.610, 0.570), lw=2.2)

    box(ax, 0.508, 0.455, 0.204, 0.110, "Policy Engine",
        "14 rules · deterministic",
        fill=POLICY_FILL, edge=POLICY_EDGE, title_size=11,
        title_color="#1a1200", sub_color="#4a3200", sub_size=7.5)

    arrow(ax, (0.508, 0.490), (0.292, 0.352), color="#22c55e", rad=0.25, lw=2.0)
    arrow(ax, (0.610, 0.455), (0.610, 0.352), color="#ef4444", lw=2.0)
    arrow(ax, (0.712, 0.490), (0.868, 0.352), color="#f59e0b", rad=-0.25, lw=2.0)

    box(ax, 0.198, 0.243, 0.188, 0.108, "ALLOW", "orchestrator + 3 workers",
        fill=ALLOW_FILL, edge=ALLOW_EDGE, title_size=11,
        title_color="#052e16", sub_color="#065f28", sub_size=7.2)
    box(ax, 0.516, 0.243, 0.188, 0.108, "BLOCK", "no action taken",
        fill=BLOCK_FILL, edge=BLOCK_EDGE, title_size=11, sub_size=7.2,
        sub_color="#fca5a5")
    box(ax, 0.766, 0.243, 0.216, 0.108, "REQUIRE_HUMAN", "human review queue",
        fill=HUMAN_FILL, edge=HUMAN_EDGE, title_size=10.5, sub_size=7.8,
        sub_color="#fcd34d")

    box(ax, 0.452, 0.055, 0.300, 0.100, "Audit Trail  +  Reconcile",
        "PostgreSQL · recovery counted only when verified",
        fill="#1b2330", edge="#93a3b8", title_size=11, sub_size=7.4)
    for x in (0.292, 0.610, 0.874):
        arrow(ax, (x, 0.243), (0.603, 0.155), color="#8b8f94", rad=0.08, lw=1.2)

    fig.savefig(path, facecolor=BG, dpi=100, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    draw_full(os.path.join(root, "architecture.png"))
    draw_simple(os.path.join(root, "architecture_simple.png"))
    print("wrote architecture.png and architecture_simple.png")
