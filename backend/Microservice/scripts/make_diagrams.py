"""Generate architecture + API-routing PNGs for the Kavachio microservices."""
import os
import re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Ellipse

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
# docs/ stays at the repo root (shared product docs), not under backend/Microservice.
OUT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/docs"

SERVICES = [
    ("auth",        8001, "#4C72B0", "/auth/*, /users*"),
    ("tenant-admin",8007, "#55A868", "/tenants*, /parties*,\n/programs*, /dashboard*"),
    ("mapper",      8002, "#C44E52", "/mapper*, /data-model,\n/bdx/sheets, /api/canonical*"),
    ("ingestion",   8003, "#8172B3", "/bdx/upload, /uploads*,\n/direct*, /dwh"),
    ("validation",  8004, "#CCB974", "/api/validate*,\n/downloads/*/decide"),
    ("export",      8005, "#64B5CD", "/export*"),
    ("contract",    8006, "#E377C2", "/programs/*/contracts*"),
]
METHOD_COLOR = {"GET": "#2E7D32", "POST": "#1565C0", "PUT": "#E65100", "DELETE": "#B71C1C", "PATCH": "#6A1B9A"}


def parse_endpoints(svc):
    p = os.path.join(ROOT, "services", svc, "app", "routes.py")
    out = []
    for m in re.finditer(r'@router\.(get|post|put|delete|patch)\("([^"]+)"', open(p).read()):
        out.append((m.group(1).upper(), m.group(2)))
    # stable sort: by path then method
    return sorted(set(out), key=lambda x: (x[1], x[0]))


# ============================================================
# 1) ARCHITECTURE / COMPONENT DIAGRAM
# ============================================================
def rbox(ax, x, y, w, h, text, fc, ec="#222", fs=11, bold=True, tc="white"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                linewidth=1.5, edgecolor=ec, facecolor=fc, zorder=3))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            fontweight="bold" if bold else "normal", color=tc, zorder=4)


def cyl(ax, x, y, w, h, text, fc):
    ax.add_patch(Ellipse((x + w / 2, y + h), w, h * 0.28, facecolor=fc, edgecolor="#222", lw=1.4, zorder=3))
    ax.add_patch(plt.Rectangle((x, y), w, h, facecolor=fc, edgecolor="none", zorder=2))
    ax.add_patch(Ellipse((x + w / 2, y), w, h * 0.28, facecolor=fc, edgecolor="#222", lw=1.4, zorder=3))
    ax.plot([x, x], [y, y + h], color="#222", lw=1.4, zorder=3)
    ax.plot([x + w, x + w], [y, y + h], color="#222", lw=1.4, zorder=3)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=10, fontweight="bold", color="white", zorder=4)


def arrow(ax, p1, p2, color="#555", ls="-", lw=1.6, rad=0.0):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=14, lw=lw,
                                 color=color, linestyle=ls, zorder=2,
                                 connectionstyle=f"arc3,rad={rad}"))


fig, ax = plt.subplots(figsize=(18, 12))
ax.set_xlim(0, 18); ax.set_ylim(0, 12); ax.axis("off")
ax.text(9, 11.6, "Kavachio — Microservices Architecture", ha="center", fontsize=23, fontweight="bold")
ax.text(9, 11.2, "React SPA → API Gateway (nginx) → 7 FastAPI services · external PostgreSQL · Redis (job status)",
        ha="center", fontsize=12, color="#555")

# Frontend + gateway (centered on x=9)
rbox(ax, 6.9, 10.2, 4.2, 0.72, "Frontend — React SPA  (:5173)", "#37474F", fs=12)
rbox(ax, 6.2, 8.95, 5.6, 0.82, "API Gateway — nginx  (:8080)\nrouting · JWT check · request-id", "#263238", fs=11)
arrow(ax, (9, 10.2), (9, 9.79), color="#333", lw=2)
ax.text(9.25, 10.0, "HTTPS", fontsize=9, color="#333")

# Service row
n = len(SERVICES); w = 2.1; x0 = 0.5; gap = (18 - 2 * x0 - n * w) / (n - 1); ytop = 7.75
centers = {}
for i, (name, port, color, prefixes) in enumerate(SERVICES):
    x = x0 + i * (w + gap); y = 6.55
    rbox(ax, x, y, w, 1.2, f"{name}\n(:{port})", color, fs=10.5)
    ax.text(x + w / 2, y - 0.28, prefixes, ha="center", va="top", fontsize=7.0, color="#333")
    centers[name] = (x + w / 2, y)
    arrow(ax, (9, 8.95), (x + w / 2, y + 1.2), color="#B0BEC5", lw=1.0, rad=0.0)

# Data stores — PostgreSQL (EXTERNAL/managed) left-of-centre, Redis right-of-centre.
# (No object store: uploaded files are stored as BYTEA blobs in PostgreSQL.)
PG_CX, RD_CX = 6.2, 11.8
cyl(ax, PG_CX - 1.45, 1.3, 2.9, 1.15, "PostgreSQL\n(:5432)  EXTERNAL / managed", "#2E5E8C")
ax.add_patch(FancyBboxPatch((PG_CX - 1.65, 1.05), 3.3, 1.75, boxstyle="round,pad=0.02",
                            fill=False, ec="#2E5E8C", lw=1.6, ls=(0, (5, 3)), zorder=1))
ax.text(PG_CX, 0.9, "outside the cluster (managed DB)", ha="center", fontsize=7.5,
        style="italic", color="#2E5E8C")
cyl(ax, RD_CX - 1.3, 1.3, 2.6, 1.15, "Redis\n(:6379)  job status", "#8C2E2E")
# every service -> external PostgreSQL (thin) — all data + file blobs live here
for name, (cx, cy) in centers.items():
    arrow(ax, (cx, cy), (PG_CX, 2.55), color="#CFD8DC", lw=0.7, rad=0.02)
# background-job status -> Redis (dashed) from services that run heavy async jobs
for name in ("mapper", "ingestion", "export"):
    cx, cy = centers[name]; arrow(ax, (cx, cy), (RD_CX, 2.55), color="#E0B4B4", lw=0.8, ls=(0, (3, 3)), rad=0.1)

# Internal service-to-service HTTP boundaries (dashed red arcs ABOVE the boxes)
def s2s(a, b, tag, rad, dy=0.0):
    (ax1, _), (bx1, _) = centers[a], centers[b]
    ax.annotate("", xy=(bx1, ytop + dy), xytext=(ax1, ytop + dy),
                arrowprops=dict(arrowstyle="-|>", color="#D32F2F", lw=2.2,
                                linestyle="dashed", connectionstyle=f"arc3,rad={rad}"), zorder=5)
    ax.text((ax1 + bx1) / 2, ytop + rad * abs(bx1 - ax1) * 0.5 + 0.08, tag,
            ha="center", fontsize=7.5, color="#D32F2F", fontweight="bold", zorder=6)

s2s("ingestion", "mapper", "LLM mapping", 0.32)
s2s("export", "validation", "DuckDB rules", 0.34)
s2s("ingestion", "validation", "DuckDB rules", 0.20, dy=0.05)

# Legend — clear top-right corner
lx, ly = 15.0, 8.75
ax.add_patch(FancyBboxPatch((lx, ly), 2.85, 2.5, boxstyle="round,pad=0.05", fc="#FAFAFA", ec="#999", lw=1, zorder=6))
ax.text(lx + 1.42, ly + 2.22, "Legend", ha="center", fontsize=11, fontweight="bold", zorder=7)
def leg(yy, col, ls, txt, lw=1.8):
    ax.plot([lx + 0.15, lx + 0.8], [yy, yy], color=col, lw=lw, ls=ls, zorder=7)
    ax.text(lx + 0.95, yy, txt, va="center", fontsize=8.2, zorder=7)
leg(ly + 1.75, "#B0BEC5", "-", "gateway → service")
leg(ly + 1.35, "#D32F2F", "--", "service → service (/internal/*)", 2.2)
leg(ly + 0.95, "#CFD8DC", "-", "service → PostgreSQL (external)")
leg(ly + 0.55, "#E0B4B4", (0, (3, 3)), "service → Redis (job status)")
ax.text(lx + 0.15, ly + 0.18, "each service owns its routes;\nshared/lib = common platform", va="center", fontsize=7.2, color="#555", zorder=7)

plt.tight_layout()
fig.savefig(os.path.join(OUT, "architecture_diagram.png"), dpi=150, bbox_inches="tight", facecolor="white")
print("wrote docs/architecture_diagram.png")

# ============================================================
# 2) API ROUTING TABLE (all endpoints, grouped by service, in columns)
# ============================================================
data = {name: parse_endpoints(name) for name, *_ in SERVICES}
maxrows = max(len(v) for v in data.values())
ncols = len(SERVICES)

fig2, ax2 = plt.subplots(figsize=(ncols * 3.75, 2.2 + maxrows * 0.34))
ax2.set_xlim(0, ncols); ax2.set_ylim(0, maxrows + 3); ax2.axis("off")
ax2.text(ncols / 2, maxrows + 2.5, "Kavachio — API → Microservice Routing",
         ha="center", fontsize=18, fontweight="bold")
ax2.text(ncols / 2, maxrows + 2.0, "All endpoints reach the API Gateway (:8080), which forwards each path to the owning service.",
         ha="center", fontsize=10, color="#555")

for ci, (name, port, color, _) in enumerate(SERVICES):
    # header
    ax2.add_patch(plt.Rectangle((ci + 0.02, maxrows + 0.6), 0.96, 0.75, color=color, zorder=2))
    ax2.text(ci + 0.5, maxrows + 0.97, f"{name}\n(:{port})", ha="center", va="center",
             fontsize=9.5, fontweight="bold", color="white", zorder=3)
    eps = data[name]
    for ri, (method, path) in enumerate(eps):
        y = maxrows + 0.2 - ri * 0.34
        if ri % 2 == 0:
            ax2.add_patch(plt.Rectangle((ci + 0.02, y - 0.15), 0.96, 0.32, color="#F4F6F8", zorder=1))
        ax2.text(ci + 0.05, y, method, fontsize=6.2, fontweight="bold",
                 color=METHOD_COLOR.get(method, "#333"), va="center", family="monospace")
        ax2.text(ci + 0.27, y, path, fontsize=6.0, color="#222", va="center", family="monospace")
    ax2.text(ci + 0.5, maxrows + 0.2 - len(eps) * 0.34 - 0.15, f"{len(eps)} endpoints",
             ha="center", fontsize=7.5, style="italic", color="#666")

# method legend
lx = 0.05
for i, (mth, col) in enumerate(METHOD_COLOR.items()):
    ax2.text(lx + i * 0.95, -0.4, "■ " + mth, color=col, fontsize=8, fontweight="bold")

plt.tight_layout()
fig2.savefig(os.path.join(OUT, "api_routing_table.png"), dpi=150, bbox_inches="tight", facecolor="white")
print("wrote docs/api_routing_table.png")
print(f"total endpoints: {sum(len(v) for v in data.values())}")
