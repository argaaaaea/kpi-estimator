import subprocess
import json
import os
from simple_salesforce import Salesforce
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

from config import SF_ALIAS

def get_current_user_id():
    """Resolve the logged-in user's SF ID from the CLI org display output."""
    sf_cmd = os.path.expandvars(r"%APPDATA%\npm\sf.cmd")
    result = subprocess.run(
        [sf_cmd, "org", "display", "-o", SF_ALIAS, "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    # 'id' is a URL like https://login.salesforce.com/id/{orgId}/{userId}
    return data["result"]["id"].rstrip("/").split("/")[-1]

# Current quarter boundaries (auto-calculated)
def get_current_quarter():
    now = datetime.now()
    m = now.month
    y = now.year
    if m <= 3:
        return datetime(y, 1, 1), datetime(y, 3, 31, 23, 59, 59)
    elif m <= 6:
        return datetime(y, 4, 1), datetime(y, 6, 30, 23, 59, 59)
    elif m <= 9:
        return datetime(y, 7, 1), datetime(y, 9, 30, 23, 59, 59)
    else:
        return datetime(y, 10, 1), datetime(y, 12, 31, 23, 59, 59)


# ── Acquisition scoring ──────────────────────────────────────────────────────

SEGMENT_POINTS = {"A": 10, "Ar": 8, "B": 5, "C": 2, "D": 0}

# Board decelerators for Upsell opportunities (CPaaS, SaaS)
BOARD_DECELERATORS = {
    "Africa":         {"CPaaS": 0.4, "SaaS": 0.8},
    "Asia and Pacific": {"CPaaS": 0.2, "SaaS": 0.6},
    "Eurasia":        {"CPaaS": 1.0, "SaaS": 1.0},
    "Europe":         {"CPaaS": 0.3, "SaaS": 0.5},
    "India":          {"CPaaS": 0.2, "SaaS": 0.6},
    "LatAm":          {"CPaaS": 0.2, "SaaS": 0.6},
    "MENA":           {"CPaaS": 0.5, "SaaS": 0.7},
    "North America":  {"CPaaS": 0.5, "SaaS": 0.5},
    "Global":         {"CPaaS": 1.0, "SaaS": 1.0},
}

NAMED_ACCOUNT_MULTIPLIER = 1.5
KPI_TARGET = 71
KPI_CAP = 130


def acquisition_points(segment, is_upsell=False, board=None, deal_type="CPaaS", is_named=False):
    base = SEGMENT_POINTS.get(segment, 0)
    if is_upsell and board:
        board_key = next((k for k in BOARD_DECELERATORS if k.lower() in board.lower()), None)
        if board_key:
            multiplier = BOARD_DECELERATORS[board_key].get(deal_type, 1.0)
            base = base * multiplier
    if is_named:
        base = base * NAMED_ACCOUNT_MULTIPLIER
    return base


# ── Delivery scoring ─────────────────────────────────────────────────────────

# Professional Services packages
PS_PACKAGES = {
    "Tune-Up":             (8, 2),
    "Training":            (14, 2),
    "Guided Launch":       (16, 5),
    "Email Premium Launch": (24, 7.5),
    "Configured Start":    (35, 10),
    "Configured Grow":     (65, 10),
    "Configured Scale":    (85, 15),
    "CX Discovery":        (20, 5),
    "CX Design":           (40, 10),
    "CX Uplift":           (80, 15),
    "ISV Partnership":     (20, 5),
    "Strategic Partnerships": (60, 10),
}

# CPaaS delivery (per channel)
CPAAS_DELIVERY = {
    "Global": (14, 2),
    "North America": (34, 5),
}

# Points by project size (total logged hours)
PROJECT_SIZE_POINTS = [
    (0, 14, 2),
    (15, 34, 5),
    (35, 74, 10),
    (75, 99, 15),
    (100, 149, 20),
    (150, 349, 50),
    (350, float("inf"), 100),
]


def delivery_points_by_hours(logged_hours):
    for low, high, pts in PROJECT_SIZE_POINTS:
        if low <= logged_hours <= high:
            return pts
    return 0


# ── Salesforce connection ─────────────────────────────────────────────────────

def get_sf_connection():
    sf_cmd = os.path.expandvars(r"%APPDATA%\npm\sf.cmd")
    result = subprocess.run(
        [sf_cmd, "org", "display", "-o", SF_ALIAS, "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    access_token = data["result"]["accessToken"]
    instance_url = data["result"]["instanceUrl"]
    return Salesforce(instance_url=instance_url, session_id=access_token)


# ── Fetch closed-won opportunities this quarter ───────────────────────────────

def get_closed_won_opps(sf, q_start, q_end):
    q_start_str = q_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    q_end_str = q_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = sf.query_all(f"""
        SELECT Id, Name, StageName, CloseDate, CurrencyIsoCode, Amount_Base__c,
               Account.Name, Type, Customer_Segment__c, Board__c,
               Opportunity_Owner_Board__c, Deal_Type__c, Segment_Points_System__c
        FROM Opportunity
        WHERE Sales_Engineer__c = '{USER_ID}'
        AND StageName = 'Closed Won'
        AND CloseDate >= {q_start.strftime("%Y-%m-%d")}
        AND CloseDate <= {q_end.strftime("%Y-%m-%d")}
        ORDER BY CloseDate DESC
    """)
    return result["records"]


# ── Fetch completed projects this quarter ─────────────────────────────────────

def get_completed_projects(sf, q_start, q_end):
    result = sf.query_all(f"""
        SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
               MPM4_BASE__Opportunity__r.Name, Scoped_Hours_PSS__c
        FROM MPM4_BASE__Milestone1_Project__c
        WHERE MPM4_BASE__Status__c = 'Completed'
        AND MPM4_BASE__Opportunity__c IN (
            SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{USER_ID}'
        )
        AND Date_Completed__c >= {q_start.strftime("%Y-%m-%d")}
        AND Date_Completed__c <= {q_end.strftime("%Y-%m-%d")}
    """)
    return result["records"]


# ── Fetch logged hours for a project (split by me vs others) ─────────────────

def get_logged_hours(sf, project_id):
    """Returns (my_hours, others_hours, others_names) for a project."""
    tasks = sf.query_all(f"""
        SELECT Id FROM MPM4_BASE__Milestone1_Task__c
        WHERE MPM4_BASE__Project_Lookup__c = '{project_id}'
    """)
    task_ids = [r["Id"] for r in tasks.get("records", [])]
    if not task_ids:
        return 0.0, 0.0, []
    id_list = ", ".join(f"'{i}'" for i in task_ids)
    entries = sf.query_all(f"""
        SELECT MPM4_BASE__Hours__c, CreatedById, CreatedBy.Name
        FROM MPM4_BASE__Milestone1_Time__c
        WHERE MPM4_BASE__Project_Task__c IN ({id_list})
    """).get("records", [])
    my_hrs = sum(e["MPM4_BASE__Hours__c"] or 0 for e in entries if e["CreatedById"] == USER_ID)
    others = {}
    for e in entries:
        if e["CreatedById"] != USER_ID:
            name = e["CreatedBy"]["Name"]
            others[name] = others.get(name, 0) + (e["MPM4_BASE__Hours__c"] or 0)
    return my_hrs, sum(others.values()), list(others.keys())


# ── Fetch active projects (for pipeline estimate) ─────────────────────────────

def get_active_projects(sf):
    result = sf.query_all(f"""
        SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
               Scoped_Hours_PSS__c
        FROM MPM4_BASE__Milestone1_Project__c
        WHERE MPM4_BASE__Status__c NOT IN ('Completed', 'Terminated')
        AND MPM4_BASE__Opportunity__c IN (
            SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{USER_ID}'
        )
        ORDER BY LastModifiedDate DESC
        LIMIT 20
    """)
    return result["records"]


# ── Display ───────────────────────────────────────────────────────────────────

console = Console()


def fmt_pts(pts):
    if pts != int(pts):
        return f"{pts:.2f}" if abs(pts - round(pts, 1)) > 0.001 else f"{pts:.1f}"
    return str(int(pts))


def print_summary(acq_rows, del_rows, active_rows, q_start, q_end):
    quarter_label = f"Q{(q_start.month - 1) // 3 + 1} {q_start.year}"

    # ── Acquisition table ─────────────────────────────────────────────────────
    console.rule(f"[bold]KPI Estimate — {quarter_label}[/bold]")
    console.print()

    t1 = Table(title="Acquisition Points (Closed Won this quarter)", box=box.SIMPLE_HEAD, show_lines=False)
    t1.add_column("Opportunity", style="cyan", no_wrap=False, max_width=45)
    t1.add_column("Account", max_width=25)
    t1.add_column("Segment", justify="center")
    t1.add_column("Type", justify="center")
    t1.add_column("Board", max_width=18)
    t1.add_column("Pts", justify="right", style="green")
    t1.add_column("Notes", max_width=30)

    total_acq = 0.0
    for r in acq_rows:
        pts = r["pts"]
        total_acq += pts
        t1.add_row(
            r["name"],
            r["account"],
            r["segment"] or "?",
            r["opp_type"] or "?",
            r["board"] or "?",
            fmt_pts(pts),
            r["notes"],
        )
    t1.add_section()
    t1.add_row("", "", "", "", "[bold]Total[/bold]", f"[bold]{fmt_pts(total_acq)}[/bold]", "")
    console.print(t1)

    # ── Delivery table (grouped by opportunity) ───────────────────────────────
    t2 = Table(title="Delivery Points (Completed projects this quarter)", box=box.SIMPLE_HEAD, show_lines=True)
    t2.add_column("Opportunity / Project", style="cyan", no_wrap=False, max_width=42)
    t2.add_column("Scoped", justify="right")
    t2.add_column("My Hrs", justify="right")
    t2.add_column("Other Hrs", justify="right")
    t2.add_column("Pts", justify="right", style="green")
    t2.add_column("Notes", max_width=32)

    total_del = 0.0
    seen_opps = {}
    for r in del_rows:
        opp_id = r["opp_id"]
        # Print opportunity header row once per opp
        if opp_id not in seen_opps:
            seen_opps[opp_id] = True
            proj_count = r["sibling_count"]
            label = f"[bold]{r['opp_name'][:40]}[/bold]"
            if proj_count > 1:
                label += f" [yellow]({proj_count} projects)[/yellow]"
            t2.add_row(label, "", "", "", "", "")
        pts = r["pts"]
        total_del += pts
        other_str = f"{r['other_hours']:.1f}" if r["other_hours"] > 0 else "-"
        t2.add_row(
            f"  {r['name'][:38]}",
            f"{r['scoped_hours']:.1f}",
            f"{r['my_hours']:.1f}",
            other_str,
            fmt_pts(pts),
            r["notes"],
        )
    t2.add_section()
    t2.add_row("", "", "", "[bold]Total[/bold]", f"[bold]{fmt_pts(total_del)}[/bold]", "")
    console.print(t2)

    # ── Pipeline table ────────────────────────────────────────────────────────
    if active_rows:
        t3 = Table(title="Active Projects (Pipeline — estimated if completed now)", box=box.SIMPLE_HEAD, show_lines=False)
        t3.add_column("Project", style="cyan", no_wrap=False, max_width=45)
        t3.add_column("Status", justify="center")
        t3.add_column("Scoped", justify="right")
        t3.add_column("My Hrs", justify="right")
        t3.add_column("Other Hrs", justify="right")
        t3.add_column("Est. Pts", justify="right", style="yellow")

        for r in active_rows:
            other_str = f"{r['other_hours']:.1f}" if r["other_hours"] > 0 else "-"
            t3.add_row(
                r["name"],
                r["status"],
                f"{r['scoped_hours']:.1f}",
                f"{r['my_hours']:.1f}",
                other_str,
                fmt_pts(r["pts"]),
            )
        console.print(t3)

    # ── Score summary ─────────────────────────────────────────────────────────
    total = min(total_acq + total_del, KPI_CAP)
    pipeline_pts = sum(r["pts"] for r in active_rows)
    projected = min(total + pipeline_pts, KPI_CAP)

    pct = (total / KPI_TARGET * 100) if KPI_TARGET else 0
    proj_pct = (projected / KPI_TARGET * 100) if KPI_TARGET else 0

    color = "green" if total >= KPI_TARGET else ("yellow" if total >= KPI_TARGET * 0.75 else "red")
    proj_color = "green" if projected >= KPI_TARGET else ("yellow" if projected >= KPI_TARGET * 0.75 else "red")

    console.print()
    console.rule("[bold]Score Summary[/bold]")
    console.print(f"  Acquisition points:   [green]{fmt_pts(total_acq)}[/green]")
    console.print(f"  Delivery points:      [green]{fmt_pts(total_del)}[/green]")
    console.print(f"  ---------------------------------")
    console.print(f"  Confirmed total:      [{color}][bold]{fmt_pts(total)}[/bold][/{color}]  /  {KPI_TARGET} target  ({pct:.0f}%)")
    if active_rows:
        console.print(f"  + Pipeline (if done): [yellow]{fmt_pts(pipeline_pts)}[/yellow]")
        console.print(f"  Projected total:      [{proj_color}][bold]{fmt_pts(projected)}[/bold][/{proj_color}]  ({proj_pct:.0f}%)  [dim](CAP: {KPI_CAP})[/dim]")
    else:
        console.print(f"  CAP: {KPI_CAP}")
    console.print()

    remaining = max(KPI_TARGET - total, 0)
    if remaining > 0:
        console.print(f"  [dim]Still need {fmt_pts(remaining)} more points to hit target.[/dim]")
    else:
        console.print(f"  [bold green]Target reached![/bold green]")
    console.print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    q_start, q_end = get_current_quarter()
    quarter_label = f"Q{(q_start.month - 1) // 3 + 1} {q_start.year}"
    print(f"Connecting to Salesforce...")
    sf = get_sf_connection()

    # ── Acquisition ───────────────────────────────────────────────────────────
    print(f"Fetching closed-won opportunities for {quarter_label}...")
    closed_opps = get_closed_won_opps(sf, q_start, q_end)
    print(f"  Found {len(closed_opps)} closed-won opportunities.")

    acq_rows = []
    for opp in closed_opps:
        segment = opp.get("Customer_Segment__c") or ""
        opp_type = opp.get("Type") or ""  # "Upsell", "New Business", etc.
        board = opp.get("Opportunity_Owner_Board__c") or opp.get("Board__c") or ""
        deal_type_raw = opp.get("Deal_Type__c") or ""
        opp_name = opp.get("Name") or ""
        is_upsell = "upsell" in opp_type.lower()
        # Determine CPaaS vs SaaS for decelerator:
        # Professional Services opportunities are treated as SaaS.
        # Otherwise fall back to Deal_Type__c; default to CPaaS if unknown.
        if "professional services" in opp_name.lower():
            deal_type = "SaaS"
        elif "saas" in deal_type_raw.lower():
            deal_type = "SaaS"
        else:
            deal_type = "CPaaS"

        pts = acquisition_points(
            segment=segment,
            is_upsell=is_upsell,
            board=board,
            deal_type=deal_type,
        )

        notes_parts = []
        if is_upsell:
            notes_parts.append(f"Upsell × decel")
        if not segment:
            notes_parts.append("No segment — check SF")
        if pts == 0 and segment not in SEGMENT_POINTS:
            notes_parts.append(f"Unknown segment '{segment}'")

        acq_rows.append({
            "name": opp.get("Name", ""),
            "account": (opp.get("Account") or {}).get("Name", ""),
            "segment": segment,
            "opp_type": opp_type,
            "board": board,
            "pts": pts,
            "notes": "; ".join(notes_parts),
        })

    # ── Delivery ──────────────────────────────────────────────────────────────
    print(f"Fetching completed projects for {quarter_label}...")
    completed_projects = get_completed_projects(sf, q_start, q_end)
    print(f"  Found {len(completed_projects)} completed projects.")

    # Build opp_id -> deal_type map for scoped hours fallback
    opp_ids_del = list({p["MPM4_BASE__Opportunity__c"] for p in completed_projects if p.get("MPM4_BASE__Opportunity__c")})
    opp_deal_type_map = {}
    if opp_ids_del:
        id_list = ", ".join(f"'{i}'" for i in opp_ids_del)
        opp_res = sf.query_all(f"SELECT Id, Name, Deal_Type__c FROM Opportunity WHERE Id IN ({id_list})")
        for o in opp_res.get("records", []):
            raw = (o.get("Deal_Type__c") or "")
            opp_name_l = (o.get("Name") or "").lower()
            if "professional services" in opp_name_l or "saas" in raw.lower():
                opp_deal_type_map[o["Id"]] = "SaaS"
            else:
                opp_deal_type_map[o["Id"]] = "CPaaS"

    # Group projects by opportunity so multi-project opps are visible
    from collections import defaultdict
    opp_proj_map = defaultdict(list)
    for proj in completed_projects:
        opp_id = proj.get("MPM4_BASE__Opportunity__c")
        opp_proj_map[opp_id].append(proj)

    del_rows = []
    for proj in completed_projects:
        print(f"  Fetching logged hours for: {proj.get('Name', '')[:50]}...")
        my_hrs, other_hrs, other_names = get_logged_hours(sf, proj["Id"])
        total_logged = my_hrs + other_hrs
        scoped = proj.get("Scoped_Hours_PSS__c")
        opp_id = proj.get("MPM4_BASE__Opportunity__c")
        opp_name = (proj.get("MPM4_BASE__Opportunity__r") or {}).get("Name", "")
        deal_type = opp_deal_type_map.get(opp_id, "CPaaS")
        sibling_count = len(opp_proj_map[opp_id])  # how many projects share this opp

        if scoped is not None:
            hours_for_bracket = scoped
            notes = f"Scoped: {scoped}h"
        else:
            hours_for_bracket = 4.0 if deal_type == "SaaS" else 2.0
            notes = f"No scoped hrs ({deal_type} fallback {hours_for_bracket}h)"

        if total_logged == 0:
            notes = "No logged hours — 0 pts per policy"
            pts = 0
        else:
            base_pts = delivery_points_by_hours(hours_for_bracket)
            my_pct = my_hrs / total_logged if total_logged > 0 else 1.0
            pts = base_pts * my_pct
            if other_hrs > 0:
                shared_str = f"Shared w/ {', '.join(other_names)} ({my_pct*100:.0f}% = {pts:.2f}pts)"
                notes = f"{notes}; {shared_str}" if notes else shared_str

        del_rows.append({
            "name": proj.get("Name", ""),
            "opp_name": opp_name,
            "opp_id": opp_id,
            "sibling_count": sibling_count,
            "status": proj.get("MPM4_BASE__Status__c", ""),
            "my_hours": my_hrs,
            "other_hours": other_hrs,
            "other_names": other_names,
            "scoped_hours": hours_for_bracket,
            "pts": pts,
            "notes": notes,
        })

    # ── Active pipeline ───────────────────────────────────────────────────────
    print(f"Fetching active projects (pipeline)...")
    active_projects = get_active_projects(sf)
    print(f"  Found {len(active_projects)} active projects.")

    opp_ids_act = list({p["MPM4_BASE__Opportunity__c"] for p in active_projects if p.get("MPM4_BASE__Opportunity__c")})
    opp_deal_type_map_act = {}
    if opp_ids_act:
        id_list = ", ".join(f"'{i}'" for i in opp_ids_act)
        opp_res2 = sf.query_all(f"SELECT Id, Name, Deal_Type__c FROM Opportunity WHERE Id IN ({id_list})")
        for o in opp_res2.get("records", []):
            raw = (o.get("Deal_Type__c") or "")
            opp_name_l = (o.get("Name") or "").lower()
            if "professional services" in opp_name_l or "saas" in raw.lower():
                opp_deal_type_map_act[o["Id"]] = "SaaS"
            else:
                opp_deal_type_map_act[o["Id"]] = "CPaaS"

    active_rows = []
    for proj in active_projects:
        my_hrs, other_hrs, other_names = get_logged_hours(sf, proj["Id"])
        total_logged = my_hrs + other_hrs
        scoped = proj.get("Scoped_Hours_PSS__c")
        opp_id = proj.get("MPM4_BASE__Opportunity__c")
        deal_type = opp_deal_type_map_act.get(opp_id, "CPaaS")

        hours_for_bracket = scoped if scoped is not None else (4.0 if deal_type == "SaaS" else 2.0)
        if total_logged > 0:
            base_pts = delivery_points_by_hours(hours_for_bracket)
            my_pct = my_hrs / total_logged if total_logged > 0 else 1.0
            pts = base_pts * my_pct
        else:
            pts = 0
        if pts > 0:
            active_rows.append({
                "name": proj.get("Name", ""),
                "status": proj.get("MPM4_BASE__Status__c", ""),
                "my_hours": my_hrs,
                "other_hours": other_hrs,
                "other_names": other_names,
                "scoped_hours": hours_for_bracket,
                "pts": pts,
            })

    print()
    print_summary(acq_rows, del_rows, active_rows, q_start, q_end)


if __name__ == "__main__":
    main()
